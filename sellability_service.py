"""Atomic local sellability transitions; never contacts marketplace APIs."""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES, canonical_key
from inventory_sync_service import inventory_sync_lease
from models import Batch, InventoryCard, InventoryChangeLog, PickAllocation, RemoteProductBinding


UNSELLABLE_REASONS = {"personal_use", "damaged", "trade", "display", "hold", "other"}
DISPOSITION_TYPES = {"local_sale", "trade", "gift", "other"}
REMOVAL_REASONS = {
    "duplicate_record", "reconciliation_error", "import_error", "scan_error",
    "inventory_count_correction", "never_owned", "other",
}


class SellabilityError(ValueError):
    pass


def _active_allocation(session: Session, card_id: int):
    return session.query(PickAllocation).filter(
        PickAllocation.inventory_card_id == card_id,
        PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES),
    ).first()


def _has_valid_identity_or_binding(session: Session, card: InventoryCard) -> bool:
    if canonical_key(card):
        return True
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
    ):
        if card.id in json.loads(binding.local_card_ids_json or "[]"):
            return True
    return False


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
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "manual_disposition",
            "previous_status": "available", "new_status": "sold",
            "disposition_type": kind, "transaction_note": note,
            "value": value, "received_description": received,
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
    card.status = "sold"
    card.sold_price = value
    card.disposition_type = kind
    card.disposition_note = note
    card.disposition_received_description = received
    card.disposed_at = timestamp.replace(tzinfo=None)
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
        if not _has_valid_identity_or_binding(session, card):
            raise SellabilityError("Card lacks a valid canonical identity or remote binding.")
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
