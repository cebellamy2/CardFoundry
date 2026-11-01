"""Bulk-move inventory cards into another batch.

★ THE RULE: ONLY AVAILABLE CARDS MAY BE BULK-MOVED, ALL OR NOTHING.
Consignment status lives at the batch level, so moving an already-sold
card would retroactively shift which consignor a past sale is attributed
to. One ineligible card -- or one id that no longer exists -- blocks the
whole move, and the caller is told exactly which cards are the problem
rather than having them silently skipped. This matches the bulk-ship
tracking gate.

Extracted from main.py's bulk_move_cards_to_batch route with no change in
behaviour: the route still owns form parsing and every page it renders;
this module owns the decision and the writes. The actor is a parameter
rather than read from the request here, so this module never imports
main.py.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from models import Batch, InventoryCard, InventoryChangeLog

TARGET_NOT_FOUND = "target_not_found"
TARGET_ARCHIVED = "target_archived"
BLOCKED = "blocked"
MOVED = "moved"


@dataclass
class BatchMoveResult:
    """What happened. `status` is one of the module constants.

    `non_available` and `missing_ids` are only filled when BLOCKED;
    `results` (one {"outcome", "name", "reason"} dict per card, in
    selection order) only when MOVED. The card objects in `non_available`
    are still bound to the caller's session, so render them before it
    closes.
    """
    status: str
    target_batch: Batch | None = None
    non_available: list[InventoryCard] = field(default_factory=list)
    missing_ids: list[int] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


def _resolve_cards(session: Session, card_ids: list[int]):
    # The same resolution main.py's _resolve_selected_cards does (dedupe,
    # keep selection order, report ids that no longer exist). Duplicated
    # rather than imported because that helper lives in main.py and is
    # shared by the other bulk routes; importing main here would make the
    # service depend on the web layer.
    unique_ids = list(dict.fromkeys(card_ids))
    cards = [
        card for card in (session.get(InventoryCard, cid) for cid in unique_ids) if card
    ]
    found_ids = {card.id for card in cards}
    missing_ids = [cid for cid in unique_ids if cid not in found_ids]
    return cards, missing_ids


def move_cards_to_batch(
    session: Session, card_ids: list[int], target_batch_id: int, *, actor: str,
) -> BatchMoveResult:
    """Move every selected card into `target_batch_id`, or none of them.

    Commits only when the move goes ahead; every refusal writes nothing.
    One InventoryChangeLog row per card that actually changed batch; a
    card already in the target is reported unchanged with no row, since
    nothing about it changed.
    """
    target_batch = session.get(Batch, target_batch_id)
    if not target_batch:
        return BatchMoveResult(status=TARGET_NOT_FOUND)
    if target_batch.is_archived:
        return BatchMoveResult(status=TARGET_ARCHIVED, target_batch=target_batch)

    cards, missing_ids = _resolve_cards(session, card_ids)

    non_available = [card for card in cards if card.status != "available"]
    if non_available or missing_ids:
        return BatchMoveResult(
            status=BLOCKED, target_batch=target_batch,
            non_available=non_available, missing_ids=missing_ids,
        )

    results = []
    for card in cards:
        old_batch = session.get(Batch, card.batch_id)
        old_batch_code = old_batch.batch_code if old_batch else str(card.batch_id)
        if card.batch_id == target_batch.id:
            results.append({
                "outcome": "unchanged", "name": card.name,
                "reason": "Already in this batch.",
            })
            continue
        card.batch_id = target_batch.id
        session.add(InventoryChangeLog(
            actor=actor,
            inventory_card_id=card.id,
            change_summary=(
                f"batch: {old_batch_code!r} -> {target_batch.batch_code!r} "
                "(bulk move)"
            ),
        ))
        results.append({
            "outcome": "moved", "name": card.name,
            "reason": f"{old_batch_code} → {target_batch.batch_code}",
        })
    session.commit()
    return BatchMoveResult(status=MOVED, target_batch=target_batch, results=results)
