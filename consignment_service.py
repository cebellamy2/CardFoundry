"""Consignment payout tier resolution, the operator-facing owed report,
and payout recording/history.

CardFoundry takes some cards on consignment rather than buying them
outright -- a cut of the sale price is owed to the consignor once (and
only once) the card actually ships. Consignment status lives on the
Batch, not the card: every InventoryCard in a consignment batch belongs
to that batch's consignor. The payout itself is a shop-wide, price-tiered
table (not a rate negotiated per consignor or per card), resolved against
the card's actual sale price -- never the estimated value captured at
intake -- so the tiers exist specifically to avoid overpaying against a
presale-hype estimate that didn't hold up.

Paying a consignor is a selectable subset, not all-or-nothing -- the
operator can hold some owed cards back for a later payout. It's a manual
ledger only; there is no real Cash App/Venmo transaction verification.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import (
    AppSetting, Batch, Consignor, ConsignorPayout, ConsignorPayoutChangeLog,
    InventoryCard,
)


CONSIGNMENT_TIERS_SETTING_KEY = "consignment_payout_tiers"

# Ordered narrowest-to-widest; the first tier whose max_price the sale
# price doesn't exceed applies. max_price=None is the catch-all top band.
# A "percent" tier may carry an optional flat "deduction" (e.g. shipping
# cost passed through to the consignor on higher-value sales) subtracted
# after the percentage is applied.
DEFAULT_CONSIGNMENT_TIERS = [
    {"max_price": 1.00, "type": "flat", "value": 0.10},
    {"max_price": 2.99, "type": "percent", "value": 0.60},
    {"max_price": 4.99, "type": "percent", "value": 0.65},
    {"max_price": 35.00, "type": "percent", "value": 0.80},
    {"max_price": None, "type": "percent", "value": 0.80, "deduction": 5.50},
]


def get_consignment_tiers(session: Session) -> list[dict]:
    setting = session.query(AppSetting).filter(
        AppSetting.key == CONSIGNMENT_TIERS_SETTING_KEY,
    ).first()
    if not setting or not setting.value:
        return DEFAULT_CONSIGNMENT_TIERS
    return json.loads(setting.value)


def set_consignment_tiers(session: Session, tiers: list[dict]) -> None:
    value = json.dumps(tiers)
    setting = session.query(AppSetting).filter(
        AppSetting.key == CONSIGNMENT_TIERS_SETTING_KEY,
    ).first()
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=CONSIGNMENT_TIERS_SETTING_KEY, value=value))


def resolve_consignment_payout(tiers: list[dict], sale_price: float) -> float:
    for tier in tiers:
        if tier["max_price"] is None or sale_price <= tier["max_price"]:
            if tier["type"] == "flat":
                return round(tier["value"], 2)
            amount = sale_price * tier["value"] - tier.get("deduction", 0)
            return round(amount, 2)
    raise ValueError("Consignment tier table has no catch-all band")


def apply_consignment_payout_if_consigned(session: Session, card: InventoryCard) -> None:
    """Call once a card ships and sold_price is set. A no-op for cards
    whose batch isn't a consignment batch. Freezes the resolved dollar
    amount onto the card -- a later tier-table edit never retroactively
    changes what an already-sold card actually paid out."""
    if card.sold_price is None:
        return
    batch = session.get(Batch, card.batch_id)
    if not batch or not batch.is_consignment:
        return
    tiers = get_consignment_tiers(session)
    card.consignment_amount_owed = resolve_consignment_payout(tiers, card.sold_price)
    card.consignment_payout_status = "owed"


def consignor_cards(session: Session, consignor_id: int) -> list[InventoryCard]:
    """Every card ever consigned by this consignor, sold or not -- the
    portal's own dashboard shows the whole relationship, not just an
    owed-only slice like the operator's report."""
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(Batch.consignor_id == consignor_id)
        .order_by(InventoryCard.name)
        .all()
    )


def consignor_owed_report(session: Session) -> list[dict]:
    """One row per consignor with any currently-owed balance, largest
    balance first. Includes inactive consignors -- a lapsed relationship
    doesn't erase money still owed."""
    consignors = session.query(Consignor).order_by(Consignor.name).all()
    report = []
    for consignor in consignors:
        owed_cards = (
            session.query(InventoryCard)
            .join(Batch, InventoryCard.batch_id == Batch.id)
            .filter(
                Batch.consignor_id == consignor.id,
                InventoryCard.consignment_payout_status == "owed",
            )
            .order_by(InventoryCard.name)
            .all()
        )
        if not owed_cards:
            continue
        total_owed = round(sum(card.consignment_amount_owed or 0 for card in owed_cards), 2)
        report.append({
            "consignor": consignor,
            "cards": owed_cards,
            "total_owed": total_owed,
        })
    report.sort(key=lambda row: row["total_owed"], reverse=True)
    return report


def record_consignor_payout(
    session: Session, consignor_id: int, card_ids: list[int],
    method: str, note: str, paid_at: datetime,
) -> ConsignorPayout:
    """Pay a selected subset of a consignor's currently-owed cards.

    Not all-or-nothing -- the operator picks which owed cards this
    payout covers. The amount is always the live sum of the selected
    cards' frozen owed amounts at commit time, never a value trusted
    from the form, so a race with another payout or a sold-price
    correction can't silently over/under-pay.
    """
    consignor = session.get(Consignor, consignor_id)
    if not consignor:
        raise ValueError("Consignor not found.")
    unique_ids = list(dict.fromkeys(card_ids))
    if not unique_ids:
        raise ValueError("Select at least one owed card to include in this payout.")
    cards = []
    for card_id in unique_ids:
        card = session.get(InventoryCard, card_id)
        if not card:
            raise ValueError(f"Inventory card {card_id} not found.")
        batch = session.get(Batch, card.batch_id)
        if not batch or batch.consignor_id != consignor_id:
            raise ValueError(f"Card {card_id} does not belong to this consignor.")
        if card.consignment_payout_status != "owed":
            raise ValueError(f"Card {card_id} is not currently owed -- it may already be paid.")
        cards.append(card)
    total = round(sum(card.consignment_amount_owed or 0 for card in cards), 2)
    payout = ConsignorPayout(
        consignor_id=consignor_id, amount=total,
        method=str(method or "").strip() or None,
        note=str(note or "").strip() or None,
        paid_at=paid_at,
    )
    session.add(payout)
    session.flush()
    for card in cards:
        card.consignment_payout_status = "paid"
        card.consignment_payout_id = payout.id
    return payout


def create_consignor_payout(
    consignor_id: int, card_ids: list[int], method: str, note: str, paid_at: datetime,
) -> dict:
    """Lease-protected payout recording; performs no external calls."""
    from database import engine
    from inventory_sync_service import inventory_sync_lease
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                payout = record_consignor_payout(
                    session, consignor_id, card_ids, method, note, paid_at,
                )
                result = {
                    "payout_id": payout.id, "amount": payout.amount,
                    "consignor_id": payout.consignor_id,
                }
            return result


def consignor_payout_history(session: Session, consignor_id: int) -> list[dict]:
    """Past payouts for one consignor, most recent first, each with the
    cards it covered."""
    payouts = (
        session.query(ConsignorPayout)
        .filter(ConsignorPayout.consignor_id == consignor_id)
        .order_by(ConsignorPayout.paid_at.desc(), ConsignorPayout.id.desc())
        .all()
    )
    history = []
    for payout in payouts:
        cards = (
            session.query(InventoryCard)
            .filter(InventoryCard.consignment_payout_id == payout.id)
            .order_by(InventoryCard.name)
            .all()
        )
        history.append({"payout": payout, "cards": cards})
    return history


def payout_state_hash(payout: ConsignorPayout) -> str:
    evidence = {
        "id": payout.id, "consignor_id": payout.consignor_id,
        "amount": payout.amount, "method": payout.method, "note": payout.note,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
    }
    return hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def correct_consignor_payout(
    session: Session, payout_id: int, expected_state_hash: str,
    new_amount: float, new_method: str, new_note: str, new_paid_at: datetime,
    correction_reason: str,
) -> ConsignorPayout:
    """Append an audited correction without rewriting the original payout
    event. Which cards a payout covers is not correctable here -- only
    the recorded amount/method/note/date. Mirrors correct_sold_price()'s
    shape: the original record is superseded in place, but the change is
    logged with a before/after and a required reason."""
    payout = session.get(ConsignorPayout, payout_id)
    if not payout:
        raise ValueError("Payout not found.")
    if payout_state_hash(payout) != expected_state_hash:
        raise ValueError("Payout changed after review.")
    cleaned_reason = str(correction_reason or "").strip()
    if not cleaned_reason:
        raise ValueError("A reason is required to correct a payout.")
    if new_amount < 0:
        raise ValueError("Corrected amount cannot be negative.")
    before = {
        "amount": payout.amount, "method": payout.method, "note": payout.note,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
    }
    payout.amount = round(new_amount, 2)
    payout.method = str(new_method or "").strip() or None
    payout.note = str(new_note or "").strip() or None
    payout.paid_at = new_paid_at
    after = {
        "amount": payout.amount, "method": payout.method, "note": payout.note,
        "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
    }
    session.add(ConsignorPayoutChangeLog(
        consignor_payout_id=payout.id,
        change_summary=json.dumps({
            "action_type": "payout_correction", "before": before, "after": after,
            "correction_reason": cleaned_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True),
    ))
    session.flush()
    return payout


def correct_payout(
    payout_id: int, expected_state_hash: str, new_amount: float, new_method: str,
    new_note: str, new_paid_at: datetime, correction_reason: str,
) -> dict:
    """Lease-protected payout correction; performs no external calls."""
    from database import engine
    from inventory_sync_service import inventory_sync_lease
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                payout = correct_consignor_payout(
                    session, payout_id, expected_state_hash, new_amount,
                    new_method, new_note, new_paid_at, correction_reason,
                )
                result = {"payout_id": payout.id, "amount": payout.amount}
            return result
