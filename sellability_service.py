"""Atomic local sellability transitions; never contacts marketplace APIs."""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from consignment_service import apply_consignment_payout_if_consigned
from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES, canonical_key
from inventory_sync_service import inventory_sync_lease
from models import Batch, InventoryCard, InventoryChangeLog, PickAllocation, RemoteProductBinding


UNSELLABLE_REASONS = {
    "personal_use", "damaged", "trade", "display", "hold", "other",
    "fulfillment_inventory_mismatch",
}
DISPOSITION_TYPES = {"local_sale", "trade", "gift", "other"}
REMOVAL_REASONS = {
    "duplicate_record", "reconciliation_error", "import_error", "scan_error",
    "inventory_count_correction", "never_owned", "other", "fulfillment_missing",
    "consignor_return", "personal_use",
    # CF-UNDO-003: an entire import (or the finalize that produced it) was
    # undone as a unit -- distinct from "import_error" (one card imported
    # wrong) so the audit trail can tell a deliberate bulk reversal apart
    # from a single-card data-entry mistake. Introduced by item 1 (reopen
    # a finalized pile), also reused by item 2 (undo a whole import).
    "import_undone",
}


class SellabilityError(ValueError):
    pass


def _active_allocation(session: Session, card_id: int):
    return session.query(PickAllocation).filter(
        PickAllocation.inventory_card_id == card_id,
        PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES),
    ).first()


def _has_canonical_identity(card: InventoryCard) -> bool:
    return canonical_key(card) is not None


def _audit(session, card, batch, action, previous, new, reason, note):
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": action,
            "previous_status": previous,
            "new_status": new,
            "reason": reason,
            "note": note,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "card_identity": {
                "name": card.name, "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id, "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
        }, sort_keys=True),
    ))


def disposition_identity_hash(card: InventoryCard) -> str:
    evidence = {
        "id": card.id, "batch_id": card.batch_id, "status": card.status,
        "name": card.name, "set_code": card.set_code,
        "collector_number": card.collector_number, "scryfall_id": card.scryfall_id,
        "mtgjson_id": card.mtgjson_id, "language_id": card.language_id,
        "condition_id": card.condition_id, "finish_id": card.finish_id,
    }
    return hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def transition_manual_disposition(
    session: Session, card_id: int, expected_status: str, expected_identity_hash: str,
    disposition_type: str, transaction_note: str, value: float | None = None,
    received_description: str | None = None,
) -> InventoryCard:
    """Guarded available-to-sold transition for non-Mana-Pool dispositions."""
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != expected_status:
        raise SellabilityError(
            f"Card status changed from reviewed {expected_status!r} to {card.status!r}."
        )
    if expected_status != "available":
        raise SellabilityError("Only an available card can be manually disposed.")
    if disposition_identity_hash(card) != expected_identity_hash:
        raise SellabilityError("Card identity or batch changed after review.")
    if _active_allocation(session, card.id):
        raise SellabilityError("Card has an active allocation and cannot be disposed manually.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Card batch no longer exists.")
    kind = str(disposition_type or "").strip().lower()
    if kind not in DISPOSITION_TYPES:
        raise SellabilityError("Select a valid disposition type.")
    note = str(transaction_note or "").strip()
    if not note:
        raise SellabilityError("Transaction note is required.")
    if value is not None and value < 0:
        raise SellabilityError("Received value cannot be negative.")
    received = str(received_description or "").strip() or None
    timestamp = datetime.now(timezone.utc)
    card.status = "sold"
    card.sold_price = value
    card.disposition_type = kind
    card.disposition_note = note
    card.disposition_received_description = received
    card.disposed_at = timestamp.replace(tzinfo=None)
    apply_consignment_payout_if_consigned(session, card)
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "manual_disposition",
            "previous_status": "available", "new_status": "sold",
            "disposition_type": kind, "transaction_note": note,
            "value": value, "received_description": received,
            "consignment_after": {
                "consignment_amount_owed": card.consignment_amount_owed,
                "consignment_payout_status": card.consignment_payout_status,
            } if batch.is_consignment else None,
            "timestamp": timestamp.isoformat(),
            "card_identity": {
                "name": card.name, "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id, "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
        }, sort_keys=True),
    ))
    session.flush()
    return card


def sold_price_state_hash(card: InventoryCard) -> str:
    evidence = {
        "identity_state_hash": disposition_identity_hash(card),
        "sold_price": card.sold_price,
    }
    return hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _original_sale_log(session: Session, card_id: int):
    for row in session.query(InventoryChangeLog).filter(
        InventoryChangeLog.inventory_card_id == card_id,
    ).order_by(InventoryChangeLog.id):
        try:
            evidence = json.loads(row.change_summary)
        except (TypeError, json.JSONDecodeError):
            continue
        if evidence.get("action_type") == "manual_disposition":
            return row
    return None


def correct_sold_price(
    session: Session, card_id: int, expected_state_hash: str,
    new_sold_price: float, reason: str,
) -> InventoryCard:
    """Append an audited correction without rewriting the sale event.

    Scoped narrowly to sold_price -- does not reopen the generic sold-card
    edit lock (name/batch/condition/etc. stay locked). Typical use: a
    partial refund issued after shipment means the price recorded at
    ship time no longer reflects what was actually kept. There is no
    Mana Pool signal for this; it is always operator-entered.
    """
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != "sold":
        raise SellabilityError("Only a sold card's sold price can be corrected.")
    if sold_price_state_hash(card) != expected_state_hash:
        raise SellabilityError("Card identity, batch, status, or sold price changed after review.")
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise SellabilityError("A reason is required to correct sold price.")
    if new_sold_price < 0:
        raise SellabilityError("Corrected sold price cannot be negative.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Card batch no longer exists.")
    original_log = _original_sale_log(session, card.id)
    original_sold_price = card.sold_price
    consignment_before = {
        "consignment_amount_owed": card.consignment_amount_owed,
        "consignment_payout_status": card.consignment_payout_status,
    }
    card.sold_price = new_sold_price
    apply_consignment_payout_if_consigned(session, card)
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "sold_price_correction",
            "before": {"sold_price": original_sold_price},
            "after": {"sold_price": new_sold_price},
            "correction_reason": cleaned_reason,
            "original_sale_log_id": original_log.id if original_log else None,
            "consignment_before": consignment_before if batch.is_consignment else None,
            "consignment_after": {
                "consignment_amount_owed": card.consignment_amount_owed,
                "consignment_payout_status": card.consignment_payout_status,
            } if batch.is_consignment else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "card_identity": {
                "name": card.name, "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id, "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
        }, sort_keys=True),
    ))
    session.flush()
    return card


def transition_inventory_removal(
    session: Session, card_id: int, expected_status: str, expected_identity_hash: str,
    removal_reason: str, removal_note: str, related_card_id: int | None = None,
) -> InventoryCard:
    """Retain a bad physical-count record while removing it from owned inventory."""
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != expected_status:
        raise SellabilityError(
            f"Card status changed from reviewed {expected_status!r} to {card.status!r}."
        )
    if expected_status != "available":
        raise SellabilityError("Only an available card can be removed from inventory.")
    if disposition_identity_hash(card) != expected_identity_hash:
        raise SellabilityError("Card identity or batch changed after review.")
    if _active_allocation(session, card.id):
        raise SellabilityError("Card has an active allocation and cannot be removed.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Card batch no longer exists.")
    reason = str(removal_reason or "").strip().lower()
    if reason not in REMOVAL_REASONS:
        raise SellabilityError("Select a valid removal reason.")
    note = str(removal_note or "").strip()
    if not note:
        raise SellabilityError("Removal note is required.")
    related = None
    if related_card_id is not None:
        if related_card_id == card.id:
            raise SellabilityError("Related card must be a different InventoryCard.")
        related = session.get(InventoryCard, related_card_id)
        if not related:
            raise SellabilityError("Related InventoryCard was not found.")
    timestamp = datetime.now(timezone.utc)
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "inventory_removal",
            "previous_status": "available", "new_status": "removed",
            "removal_reason": reason, "removal_note": note,
            "related_inventory_card_id": related.id if related else None,
            "timestamp": timestamp.isoformat(),
            "card_identity": {
                "name": card.name, "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id, "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
            "import_record_id": card.import_id,
        }, sort_keys=True),
    ))
    card.status = "removed"
    card.removal_reason = reason
    card.removal_note = note
    card.removal_related_inventory_card_id = related.id if related else None
    card.removed_at = timestamp.replace(tzinfo=None)
    session.flush()
    return card


def removal_metadata_state_hash(card: InventoryCard) -> str:
    evidence = {
        "identity_state_hash": disposition_identity_hash(card),
        "removal_reason": card.removal_reason,
        "removal_note": card.removal_note,
        "related_inventory_card_id": card.removal_related_inventory_card_id,
        "removed_at": card.removed_at,
    }
    return hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _original_removal_log(session: Session, card_id: int):
    for row in session.query(InventoryChangeLog).filter(
        InventoryChangeLog.inventory_card_id == card_id,
    ).order_by(InventoryChangeLog.id):
        try:
            evidence = json.loads(row.change_summary)
        except (TypeError, json.JSONDecodeError):
            continue
        if evidence.get("action_type") == "inventory_removal":
            return row
    return None


def correct_removal_metadata(
    session: Session, card_id: int, expected_state_hash: str,
    removal_reason: str, removal_note: str, related_card_id: int | None,
    correction_reason: str,
) -> InventoryCard:
    """Append an audited correction without rewriting the removal event."""
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != "removed":
        raise SellabilityError("Only a removed card can have removal details corrected.")
    if removal_metadata_state_hash(card) != expected_state_hash:
        raise SellabilityError("Removed-card identity, batch, status, or metadata changed after review.")
    reason = str(removal_reason or "").strip().lower()
    if reason not in REMOVAL_REASONS:
        raise SellabilityError("Select a valid removal reason.")
    note = str(removal_note or "").strip()
    if not note:
        raise SellabilityError("Removal note is required.")
    rationale = str(correction_reason or "").strip()
    if not rationale:
        raise SellabilityError("Correction reason is required.")
    related = None
    if related_card_id is not None:
        if related_card_id == card.id:
            raise SellabilityError("Related card must be a different InventoryCard.")
        related = session.get(InventoryCard, related_card_id)
        if not related:
            raise SellabilityError("Related InventoryCard was not found.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Original batch no longer exists.")
    original_log = _original_removal_log(session, card.id)
    before = {
        "removal_reason": card.removal_reason,
        "removal_note": card.removal_note,
        "related_inventory_card_id": card.removal_related_inventory_card_id,
    }
    after = {
        "removal_reason": reason, "removal_note": note,
        "related_inventory_card_id": related.id if related else None,
    }
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "removal_metadata_correction",
            "before": before, "after": after,
            "correction_reason": rationale,
            "original_inventory_removal_log_id": original_log.id if original_log else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "card_identity": {
                "name": card.name, "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id, "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
            "import_record_id": card.import_id,
        }, sort_keys=True),
    ))
    card.removal_reason = reason
    card.removal_note = note
    card.removal_related_inventory_card_id = related.id if related else None
    session.flush()
    return card


def transition_sellability(
    session: Session, card_id: int, expected_status: str,
    target_status: str, reason: str | None = None, note: str | None = None,
) -> InventoryCard:
    """Re-read and transition one card inside the caller's transaction."""
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != expected_status:
        raise SellabilityError(
            f"Card status changed from reviewed {expected_status!r} to {card.status!r}."
        )
    allocation = _active_allocation(session, card.id)
    if allocation:
        raise SellabilityError("Card has an active allocation and cannot be changed manually.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Card batch no longer exists.")

    if target_status == "unsellable":
        if expected_status != "available":
            raise SellabilityError("Only an available card can be marked Not For Sale.")
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason not in UNSELLABLE_REASONS:
            raise SellabilityError("Select a valid Not For Sale reason.")
        cleaned_note = str(note or "").strip() or None
        _audit(session, card, batch, "mark_unsellable", card.status, target_status,
               normalized_reason, cleaned_note)
        card.status = "unsellable"
        card.unsellable_reason = normalized_reason
        card.unsellable_note = cleaned_note
        card.unsellable_at = datetime.now()
    elif target_status == "available":
        if expected_status != "unsellable":
            raise SellabilityError("Only a Not For Sale card can return to sellable inventory.")
        if batch.is_archived:
            raise SellabilityError("Archived-batch cards cannot return to sellable inventory.")
        if not _has_canonical_identity(card):
            raise SellabilityError(
                "Card lacks a canonical MTGJSON identity and cannot return to sellable inventory."
            )
        prior_reason, prior_note = card.unsellable_reason, card.unsellable_note
        _audit(session, card, batch, "return_to_sellable", card.status, target_status,
               prior_reason, prior_note)
        card.status = "available"
        card.unsellable_reason = None
        card.unsellable_note = None
        card.unsellable_at = None
    else:
        raise SellabilityError("Unsupported sellability transition.")
    session.flush()
    return card


def change_sellability(card_id: int, expected_status: str, target_status: str,
                       reason: str | None = None, note: str | None = None):
    """Lease-protected, atomic local operation with no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = transition_sellability(
                    session, card_id, expected_status, target_status, reason, note,
                )
                result = {"card_id": card.id, "status": card.status}
            return result


def dispose_card_locally(
    card_id: int, expected_status: str, expected_identity_hash: str,
    disposition_type: str, transaction_note: str, value: float | None = None,
    received_description: str | None = None,
):
    """Lease-protected atomic local disposition; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = transition_manual_disposition(
                    session, card_id, expected_status, expected_identity_hash,
                    disposition_type, transaction_note, value, received_description,
                )
                result = {"card_id": card.id, "status": card.status}
            return result


def remove_card_from_inventory(
    card_id: int, expected_status: str, expected_identity_hash: str,
    removal_reason: str, removal_note: str, related_card_id: int | None = None,
):
    """Lease-protected atomic local correction; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = transition_inventory_removal(
                    session, card_id, expected_status, expected_identity_hash,
                    removal_reason, removal_note, related_card_id,
                )
                result = {"card_id": card.id, "status": card.status}
            return result


def amend_removal_metadata(
    card_id: int, expected_state_hash: str, removal_reason: str,
    removal_note: str, related_card_id: int | None, correction_reason: str,
):
    """Lease-protected append-only correction; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = correct_removal_metadata(
                    session, card_id, expected_state_hash, removal_reason,
                    removal_note, related_card_id, correction_reason,
                )
                result = {"card_id": card.id, "status": card.status}
            return result


def transition_card_un_removal(
    session: Session, card_id: int, expected_identity_hash: str, undo_note: str,
) -> InventoryCard:
    """CF-UNDO-001: guarded removed -> available reversal, the direct
    counterpart to transition_inventory_removal(). Follows reopen_pick_
    wave's own three-part shape (pick_wave_service.py): (1) all-or-
    nothing, guarded on exact prior state via the SAME removal_metadata_
    state_hash() correct_removal_metadata already uses -- refuses
    outright if anything drifted since the operator reviewed it; (2)
    purely local, nothing to retract externally (removal itself never
    contacted Mana Pool either); (3) writes its own InventoryChangeLog
    audit row capturing exactly what was reverted, rather than silently
    erasing the removal's own trail.

    Guarded the same way transition_sellability's own unsellable-
    ->available direction already is (no active allocation, batch not
    archived, canonical identity intact) -- a removed card should never
    legitimately fail any of these, but the checks cost nothing and keep
    this symmetric with every other "return to sellable inventory" path
    in this file, rather than inventing a laxer one just for this case.
    """
    card = session.get(InventoryCard, card_id)
    if not card:
        raise SellabilityError("Inventory card not found.")
    if card.status != "removed":
        raise SellabilityError(f"Only a removed card can be un-removed (this card is {card.status!r}).")
    if removal_metadata_state_hash(card) != expected_identity_hash:
        raise SellabilityError("Card identity or removal metadata changed after review.")
    if _active_allocation(session, card.id):
        raise SellabilityError("Card has an active allocation and cannot be un-removed.")
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise SellabilityError("Card batch no longer exists.")
    if batch.is_archived:
        raise SellabilityError("Archived-batch cards cannot return to sellable inventory.")
    if not _has_canonical_identity(card):
        raise SellabilityError(
            "Card lacks a canonical MTGJSON identity and cannot return to sellable inventory."
        )
    cleaned_note = str(undo_note or "").strip()
    if not cleaned_note:
        raise SellabilityError("A reason is required to undo a removal.")

    prior_reason = card.removal_reason
    _audit(session, card, batch, "un_remove", "removed", "available", prior_reason, cleaned_note)
    card.status = "available"
    card.removal_reason = None
    card.removal_note = None
    card.removal_related_inventory_card_id = None
    card.removed_at = None
    session.flush()
    return card


def un_remove_card(card_id: int, expected_identity_hash: str, undo_note: str):
    """Lease-protected atomic reversal; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = transition_card_un_removal(session, card_id, expected_identity_hash, undo_note)
                result = {"card_id": card.id, "status": card.status}
            return result


def remove_cards_by_import(session: Session, import_id: int, note: str) -> dict:
    """CF-UNDO-003 item 2: undo a whole import, one card at a time through
    the SAME guarded removal transition_inventory_removal already uses for
    a single card -- not a special bulk-only path, so every existing guard
    (available-only, active-allocation, valid reason/note) applies per
    card exactly as it always has.

    Deliberately best-effort, unlike reopen_finalized_pile's all-or-
    nothing: a card already allocated/sold/otherwise moved on is skipped
    with a reason, and every OTHER card in the import is still removed --
    "don't let one blocked card silently block the rest," per the ticket.
    Never partially removes a single card; each one either fully succeeds
    through transition_inventory_removal or is skipped untouched.
    """
    cards = (
        session.query(InventoryCard)
        .filter(InventoryCard.import_id == import_id)
        .order_by(InventoryCard.id)
        .all()
    )
    removed = []
    skipped = []
    for card in cards:
        if card.status != "available":
            skipped.append({
                "card_id": card.id, "name": card.name,
                "reason": f"Card is {card.status!r}, not available.",
            })
            continue
        try:
            with session.begin_nested():
                transition_inventory_removal(
                    session, card.id, "available", disposition_identity_hash(card),
                    "import_undone", note,
                )
            removed.append({"card_id": card.id, "name": card.name})
        except SellabilityError as exc:
            skipped.append({"card_id": card.id, "name": card.name, "reason": str(exc)})
    return {"removed": removed, "skipped": skipped}


def remove_import_cards(import_id: int, note: str) -> dict:
    """Lease-protected atomic bulk removal; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                result = remove_cards_by_import(session, import_id, note)
            return result


def correct_card_sold_price(
    card_id: int, expected_state_hash: str, new_sold_price: float, reason: str,
):
    """Lease-protected sold-price correction; performs no external calls."""
    from database import engine
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                card = correct_sold_price(
                    session, card_id, expected_state_hash, new_sold_price, reason,
                )
                result = {"card_id": card.id, "sold_price": card.sold_price}
            return result


def sellable_remote_product_ids(session: Session, remote_inventory: list[dict]) -> set[str]:
    """Map currently available local cards to exact remote product IDs."""
    available_ids = {
        card.id for card in session.query(InventoryCard).join(Batch).filter(
            InventoryCard.status == "available", Batch.is_archived == False,
        )
    }
    products = set()
    remote_by_key = {}
    for item in remote_inventory:
        single = ((item.get("product") or {}).get("single") or {})
        key = tuple(str(single.get(field) or "").strip().upper() for field in (
            "mtgjson_id", "language_id", "condition_id", "finish_id",
        ))
        if all(key) and item.get("product_id"):
            remote_by_key[key] = str(item["product_id"])
    for card in session.query(InventoryCard).filter(InventoryCard.id.in_(available_ids)):
        key = canonical_key(card)
        if key and key in remote_by_key:
            products.add(remote_by_key[key])
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
    ):
        if available_ids.intersection(json.loads(binding.local_card_ids_json or "[]")):
            products.add(binding.product_id)
    return products
