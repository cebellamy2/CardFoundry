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
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from actor_context import current_actor
from sqlalchemy.orm import Session

from models import (
    AppSetting, Batch, Consignor, ConsignorChangeLog, ConsignorPayout,
    ConsignorPayoutChangeLog, InventoryCard, InventoryChangeLog,
)

# Same shared logger as the rest of the app (v1.155.0).
logger = logging.getLogger("cardfoundry")


class ConsignorChangeError(ValueError):
    pass


CONSIGNMENT_TIERS_SETTING_KEY = "consignment_payout_tiers"

# Ordered narrowest-to-widest; the first tier whose max_price the sale
# price doesn't exceed applies. max_price=None is the catch-all top band.
# A "percent" tier may carry an optional flat "deduction" (e.g. shipping
# cost passed through to the consignor on higher-value sales) subtracted
# after the percentage is applied.
DEFAULT_CONSIGNMENT_TIERS = [
    # Retired 2026-09-23, operator decision: a card that sells under a
    # dollar now pays the consignor nothing, matching the buy side's own
    # under-$1 tier (flat $0.00). It previously paid a flat $0.10, which
    # across 135 already-sold cards came to $16.58 total -- the handling
    # cost more than the payout. Existing consignment_amount_owed values
    # are NOT rewritten: those are settled facts, and 28 of those cards
    # are already paid. This changes what future sales resolve to.
    {"max_price": 1.00, "type": "flat", "value": 0.00},
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
    # ★ ONGOING ZERO SETTLEMENT. Every live path that resolves an amount
    # on a sale comes through here -- shipping, both sellability routes,
    # and the backfills that reuse this function -- so a card the tier
    # table resolves to nothing never gets a chance to sit in "owed".
    # Scoped to this one card, so it can never sweep up anything the
    # caller did not touch.
    if card.consignment_amount_owed == 0:
        settle_zero_owed_cards(
            session, reason="resolved to $0.00 owed on sale", card_ids=[card.id],
        )


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


RECONCILIATION_TOLERANCE = 0.005


def payout_corrections(session: Session, payout_id: int) -> list[dict]:
    """Every audited correction for one payout, oldest first.

    A row whose change_summary will not parse is reported as such rather
    than dropped -- an unreadable audit row is itself information, and
    silently skipping it would make a corrected payout look untouched.
    """
    corrections = []
    logs = (
        session.query(ConsignorPayoutChangeLog)
        .filter(ConsignorPayoutChangeLog.consignor_payout_id == payout_id)
        .order_by(ConsignorPayoutChangeLog.created_at, ConsignorPayoutChangeLog.id)
        .all()
    )
    for log in logs:
        try:
            parsed = json.loads(log.change_summary or "{}")
        except (TypeError, ValueError):
            logger.warning(
                "Payout %s change log %s has unreadable change_summary; "
                "surfacing it as unreadable rather than hiding the correction.",
                payout_id, log.id,
            )
            corrections.append({
                "id": log.id, "created_at": log.created_at, "readable": False,
                "before": {}, "after": {}, "reason": None,
            })
            continue
        corrections.append({
            "id": log.id,
            "created_at": log.created_at,
            "readable": True,
            "before": parsed.get("before") or {},
            "after": parsed.get("after") or {},
            "reason": parsed.get("correction_reason"),
        })
    return corrections


def payout_detail(session: Session, payout_id: int) -> dict | None:
    """Everything one payout's detail page shows, gathered once so the
    operator route and the portal route cannot drift.

    Returns None when the payout does not exist -- the caller decides what
    a refusal looks like (the portal deliberately gives the same answer for
    "not yours" as for "does not exist").

    THE NOTE "AS ENTERED AT THE TIME". correct_consignor_payout() edits
    payout.note IN PLACE, so the original survives only inside the FIRST
    correction's `before` blob. So:
      * no corrections  -> payout.note IS the note as entered.
      * corrections     -> the earliest correction's before.note is it.
    ``note_at_record_time_is_certain`` is False only when a correction
    exists but its before blob is unreadable. What this CANNOT detect is a
    note changed by anything other than correct_consignor_payout (a direct
    script or SQL edit), which writes no log at all -- such an edit is
    indistinguishable from the original here, and is reported as the
    original.
    """
    payout = session.get(ConsignorPayout, payout_id)
    if not payout:
        return None
    consignor = session.get(Consignor, payout.consignor_id)
    cards = (
        session.query(InventoryCard)
        .filter(InventoryCard.consignment_payout_id == payout.id)
        .order_by(InventoryCard.name, InventoryCard.id)
        .all()
    )
    corrections = payout_corrections(session, payout.id)

    cards_total = round(sum(card.consignment_amount_owed or 0 for card in cards), 2)
    payout_amount = round(payout.amount or 0, 2)

    note_at_record_time = payout.note
    note_certain = True
    if corrections:
        first = corrections[0]
        if first["readable"] and "note" in first["before"]:
            note_at_record_time = first["before"].get("note")
        else:
            note_certain = False

    return {
        "payout": payout,
        "consignor": consignor,
        "consignor_name": consignor.name if consignor else None,
        "cards": cards,
        "corrections": corrections,
        "cards_total": cards_total,
        "payout_amount": payout_amount,
        "reconciles": abs(cards_total - payout_amount) <= RECONCILIATION_TOLERANCE,
        "difference": round(cards_total - payout_amount, 2),
        "note_at_record_time": note_at_record_time,
        "note_at_record_time_is_certain": note_certain,
        "was_corrected": bool(corrections),
    }


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


def _consignor_snapshot(consignor: Consignor) -> dict:
    return {
        "name": consignor.name, "contact_info": consignor.contact_info,
        "payout_method": consignor.payout_method, "is_active": consignor.is_active,
    }


def create_consignor_with_log(
    session: Session, name: str, contact_info: str | None, payout_method: str | None,
) -> Consignor:
    """CF-UNDO-003 item 3b: consignor create/edit had no change log at all
    -- same freeform-JSON shape as ConsignorPayoutChangeLog above, just
    scoped to name/contact_info/payout_method/is_active. A create's own
    entry has no "before" (nothing existed yet) and is never itself
    revertible -- reverting a create would just be deleting the consignor,
    which this app deliberately never does -- only edit entries are.
    """
    consignor = Consignor(name=name, contact_info=contact_info, payout_method=payout_method)
    session.add(consignor)
    session.flush()
    session.add(ConsignorChangeLog(
        consignor_id=consignor.id,
        change_summary=json.dumps({
            "action_type": "consignor_created", "after": _consignor_snapshot(consignor),
        }, sort_keys=True),
    ))
    session.flush()
    return consignor


def update_consignor_with_log(
    session: Session, consignor_id: int, name: str, contact_info: str | None,
    payout_method: str | None, is_active: bool,
) -> Consignor:
    consignor = session.get(Consignor, consignor_id)
    if not consignor:
        raise ConsignorChangeError("Consignor not found.")
    before = _consignor_snapshot(consignor)
    consignor.name = name
    consignor.contact_info = contact_info
    consignor.payout_method = payout_method
    consignor.is_active = is_active
    after = _consignor_snapshot(consignor)
    if before != after:
        session.add(ConsignorChangeLog(
            consignor_id=consignor.id,
            change_summary=json.dumps({
                "action_type": "consignor_updated", "before": before, "after": after,
            }, sort_keys=True),
        ))
    session.flush()
    return consignor


def consignor_change_history(session: Session, consignor_id: int) -> list[dict]:
    logs = (
        session.query(ConsignorChangeLog)
        .filter(ConsignorChangeLog.consignor_id == consignor_id)
        .order_by(ConsignorChangeLog.created_at.desc(), ConsignorChangeLog.id.desc())
        .all()
    )
    return [{"log": log, "entry": json.loads(log.change_summary)} for log in logs]


def revert_consignor_change(session: Session, consignor_id: int, log_id: int) -> Consignor:
    """All-or-nothing, guarded on exact prior state -- same shape as every
    other CF-UNDO reversal: refuses if the consignor has changed again
    since this specific edit (the log's own "after" snapshot no longer
    matches current values), rather than guessing which fields to touch.
    Only a "consignor_updated" entry is revertible -- not a create (see
    create_consignor_with_log) and not a previous revert of a revert,
    which would need its own explicit support this doesn't add.
    """
    consignor = session.get(Consignor, consignor_id)
    if not consignor:
        raise ConsignorChangeError("Consignor not found.")
    log = session.get(ConsignorChangeLog, log_id)
    if not log or log.consignor_id != consignor_id:
        raise ConsignorChangeError("Change log entry not found for this consignor.")
    entry = json.loads(log.change_summary)
    if entry.get("action_type") != "consignor_updated":
        raise ConsignorChangeError("Only an edit entry can be reverted.")
    before = entry["before"]
    after = entry["after"]
    if _consignor_snapshot(consignor) != after:
        raise ConsignorChangeError(
            "This consignor has changed again since this edit -- cannot revert cleanly."
        )
    consignor.name = before["name"]
    consignor.contact_info = before["contact_info"]
    consignor.payout_method = before["payout_method"]
    consignor.is_active = before["is_active"]
    session.add(ConsignorChangeLog(
        consignor_id=consignor.id,
        change_summary=json.dumps({
            "action_type": "consignor_edit_reverted", "reverted_log_id": log.id,
            "before": after, "after": before,
        }, sort_keys=True),
    ))
    session.flush()
    return consignor


# --- audited per-card amount-owed correction ---------------------------
#
# apply_consignment_payout_if_consigned deliberately FREEZES the resolved
# amount at sale time, so a later tier-table edit never retroactively
# changes what an already-sold card paid out. That freeze is correct and
# is NOT touched. This is the explicit, operator-authorised override for
# the cases where the frozen number has to change anyway -- it takes a
# named card set and a reason, never a query, so it can only ever do what
# an operator asked for.
#
# Mirrors correct_consignor_payout above: the value is superseded in
# place, and the change is logged with a before/after and a required
# reason. The audit lands in InventoryChangeLog, which already carries
# arbitrary change_summary JSON for exactly this and needed no schema
# change.
CONSIGNMENT_AMOUNT_CORRECTION_ACTION = "consignment_amount_correction"
CONSIGNMENT_AMOUNT_CORRECTION_UNDO_ACTION = "consignment_amount_correction_undo"


class ConsignmentCorrectionError(ValueError):
    """A correction was refused. Nothing is written when this is raised."""


def _correction_refusal(card, card_id: int) -> str | None:
    """Why this card may not have its amount owed corrected, or None."""
    if card is None:
        return f"card {card_id} not found"
    if card.consignment_payout_id is not None:
        return (f"card {card_id} is attached to payout "
                f"{card.consignment_payout_id} -- correct paid money through "
                f"the payout-correction route, not here")
    if str(card.consignment_payout_status or "").strip().lower() == "paid":
        return f"card {card_id} is already paid"
    return None


def correct_consignment_amounts(
    session: Session, card_ids, new_amount: float, reason: str,
    *, dry_run: bool = False, action: str = CONSIGNMENT_AMOUNT_CORRECTION_ACTION,
) -> dict:
    """Set consignment_amount_owed on an EXPLICIT set of consigned cards.

    All-or-nothing: every card is checked first, and if any is refused
    nothing at all is written. A dry run returns the same per-card
    before/after and writes nothing.

    Refuses a card that is not consigned, that is already paid, or that
    is attached to a payout record -- paid money is corrected only
    through correct_consignor_payout. Refuses an empty card set and an
    empty reason; there is deliberately no "all cards" mode.
    """
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ConsignmentCorrectionError(
            "A reason is required to correct a consignment amount.")
    ids = list(dict.fromkeys(int(value) for value in (card_ids or [])))
    if not ids:
        raise ConsignmentCorrectionError(
            "No cards selected. This never operates on an implicit set.")
    if new_amount is None or float(new_amount) < 0:
        raise ConsignmentCorrectionError("Corrected amount cannot be negative.")
    amount = round(float(new_amount), 2)

    refusals, planned = [], []
    for card_id in ids:
        card = session.get(InventoryCard, card_id)
        refusal = _correction_refusal(card, card_id)
        if refusal is None:
            batch = session.get(Batch, card.batch_id)
            if not batch or not batch.is_consignment:
                refusal = f"card {card_id} is not in a consignment batch"
        if refusal:
            refusals.append(refusal)
            continue
        planned.append({
            "inventory_card_id": card.id,
            "name": card.name,
            "sold_price": card.sold_price,
            "before": card.consignment_amount_owed,
            "after": amount,
            "payout_status": card.consignment_payout_status,
        })

    if refusals:
        # All-or-nothing. Logged loudly: a refused correction is an
        # operator asking for something the ledger will not allow, and
        # that should be visible rather than only returned.
        logger.warning(
            "consignment amount correction REFUSED, nothing written: %s of %s "
            "card(s) ineligible: %s",
            len(refusals), len(ids), "; ".join(refusals[:10]),
        )
        raise ConsignmentCorrectionError(
            f"{len(refusals)} of {len(ids)} card(s) cannot be corrected, so "
            f"nothing was changed: " + "; ".join(refusals[:10])
        )

    report = {
        "action": action, "reason": cleaned_reason, "new_amount": amount,
        "dry_run": bool(dry_run), "cards": planned,
        "count": len(planned),
        "total_before": round(sum(p["before"] or 0 for p in planned), 2),
        "total_after": round(amount * len(planned), 2),
    }
    if dry_run:
        return report

    timestamp = datetime.now(timezone.utc).isoformat()
    for plan in planned:
        card = session.get(InventoryCard, plan["inventory_card_id"])
        card.consignment_amount_owed = amount
        session.add(InventoryChangeLog(
            actor=current_actor(),
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": action,
                "before": {"consignment_amount_owed": plan["before"]},
                "after": {"consignment_amount_owed": amount},
                "correction_reason": cleaned_reason,
                "operator_authorised": True,
                "sold_price": plan["sold_price"],
                "timestamp": timestamp,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info(
        "consignment amount correction: %s card(s) set to $%.2f (was $%.2f "
        "total) -- %s", len(planned), amount, report["total_before"],
        cleaned_reason,
    )
    # A correction down to nothing settles those cards, same as a sale
    # that resolves to nothing. Scoped to the cards this correction
    # touched, never the whole table.
    if amount == 0 and planned:
        report["zero_settlement"] = settle_zero_owed_cards(
            session, reason=f"corrected to $0.00 owed: {cleaned_reason}",
            card_ids=[p["inventory_card_id"] for p in planned],
        )
    return report


# Provenance for a consigned card whose record came from the operator's
# own Google Sheets rather than through CardFoundry's intake
# (import_consignment_sheets.py). The overdue rule keys on THIS, never on
# "we could not find a sale date" -- a card that is merely missing a date
# must not quietly start counting as overdue.
LEGACY_SHEET_IMPORT_SOURCE = "google_sheets_backfill"


# ---------------------------------------------------------------------
# THE ONE OVERDUE RULE, and the date arithmetic behind it
# ---------------------------------------------------------------------
#
# Moved here from main.py (2026-10-27) because a second caller needed it:
# the payout page's "Select only past-due" control. The rule already
# answered two questions -- the consignor portal's Overdue marker and
# What's Owed's Amount past due column -- and the operator's instruction
# has always been that there is exactly one of it. A route that wanted it
# from a service module could not import main.py, and copying it would
# have created the second rule this comment exists to prevent, so it
# moved down to sit beside LEGACY_SHEET_IMPORT_SOURCE, which it keys on.
#
# main.py imports these under its own long-standing private names, so its
# call sites are untouched.

# Operator decision, 2026-09-10: "hold me accountable to how long it's
# taking to pay people out." The consignor portal's own timezone for
# every date on this ticket -- sold date, and the day-of-week math that
# derives expected payout date from it.
PAYOUT_TIMEZONE = ZoneInfo("America/New_York")

def expected_payout_date(sold_at_utc: datetime) -> date:
    """Operator's own rule, confirmed with a worked example (sold Wed
    Sept 2 -> +7 = Wed Sept 9 -> expected Thu Sept 10): the sold date
    (America/New_York calendar day, not UTC -- see PAYOUT_TIMEZONE) plus
    7 days, then forward to the next Tuesday or Thursday, whichever
    comes first. A +7 landing already on Tuesday or Thursday IS the
    expected date -- never skipped past."""
    sold_local_date = sold_at_utc.replace(tzinfo=timezone.utc).astimezone(PAYOUT_TIMEZONE).date()
    candidate = sold_local_date + timedelta(days=7)
    while candidate.weekday() not in (1, 3):  # Monday=0 ... Tuesday=1, Thursday=3
        candidate += timedelta(days=1)
    return candidate

def payout_today_local() -> date:
    """Today, in the terms the expected-payout rule is defined in.

    Its own function so the portal marker and the What's Owed column
    cannot end up asking different calendars what day it is -- and so a
    test can pin "today" in one place rather than patching a clock in
    two.
    """
    return datetime.now(timezone.utc).astimezone(PAYOUT_TIMEZONE).date()

# A manual disposition that is NOT a sale, so its date is not a sale date.
# "gift" is the only type in the vocabulary that definitionally moves a card
# without a sale; local_sale, trade and other all record something received,
# and the value check below is what proves it in each case.
NON_SALE_DISPOSITION_TYPES = frozenset({"gift"})


def local_sale_date(card: "InventoryCard"):
    """When a card left by hand, if it left as a SALE.

    The consignor portal and What's Owed both need a sale moment, and a
    card sold across the counter has no Mana Pool order to take one from
    -- but it does carry the moment the operator recorded the sale. This
    is that date, and it is read straight off the card, so it costs no
    query at all.

    ★ IT MUST BE A SALE, not merely a disposition. Two gates, because
    the label alone is not enough: the type is not one of the non-sale
    kinds, AND a value was actually recorded. A disposition entered with
    no value received is not a sale whatever it is called, and treating
    its date as a payout deadline would invent a debt.
    """
    if card.disposed_at is None or card.sold_price is None:
        return None
    if str(card.disposition_type or "").strip().lower() in NON_SALE_DISPOSITION_TYPES:
        return None
    return card.disposed_at


def payout_is_overdue(card: "InventoryCard", sold_at, today_local=None) -> bool:
    """★ THE ONE OVERDUE RULE. The operator: "past due and overdue are
    the same." So the consignor portal's Overdue marker and What's Owed's
    Amount past due column are the SAME question, and they are answered
    here rather than in two places that could drift.

    Overdue means: the card sold, it has not been paid out, and today
    (America/New_York) is past its expected payout date --
    expected_payout_date's own rule, unchanged.

    ★ A LEGACY IMPORTED CARD WITH NO SALE DATE IS OVERDUE (operator,
    2026-10-17). Those records came out of his own sheets and have been
    sitting in CardFoundry far longer than any payout window, so treating
    them as "cannot say" understated what he actually owes. It applies to
    every consignor; there is no per-consignor exception.

    IT KEYS ON THE CARD'S OWN IMPORT SOURCE, never on "we could not find
    a date". That distinction is the whole safety property: a card that
    merely has a gap -- a sale we failed to link, a future import that
    forgets something -- must NOT quietly start counting as money overdue.
    Only a card explicitly stamped as a legacy sheet import qualifies, and
    nothing else sets that stamp.

    Any other card whose sale moment cannot be resolved is still NOT
    counted overdue: its expected date is unknowable, so calling it past
    due would assert a deadline that was never computed.
    """
    if card.status != "sold":
        return False
    if card.consignment_payout_status == "paid":
        return False
    # The shipped order first, then a real local sale, and only when
    # neither exists does the legacy stamp decide. A card sold across the
    # counter has a genuine date; it should be judged on that date rather
    # than swept in with the records that have none.
    effective_sale = sold_at if sold_at is not None else local_sale_date(card)
    if effective_sale is None:
        return card.consignment_import_source == LEGACY_SHEET_IMPORT_SOURCE
    today_local = today_local or payout_today_local()
    return today_local > expected_payout_date(effective_sale)


ZERO_SETTLEMENT_ACTION = "consignment_zero_settlement"
ZERO_SETTLEMENT_UNDO_ACTION = "consignment_zero_settlement_undo"


def zero_owed_candidates(session: Session, card_ids=None) -> list[InventoryCard]:
    """Consigned cards sitting at exactly $0.00 owed.

    Exactly zero, never "less than a cent": a card owed $0.01 is a real
    debt and is left alone. NULL is not zero either -- it means no amount
    has been resolved yet, which is a different thing from one resolved
    to nothing.
    """
    query = session.query(InventoryCard).filter(
        InventoryCard.consignment_payout_status == "owed",
        InventoryCard.consignment_amount_owed == 0,
    )
    if card_ids is not None:
        ids = list(card_ids)
        if not ids:
            return []
        query = query.filter(InventoryCard.id.in_(ids))
    return query.order_by(InventoryCard.id).all()


def settle_zero_owed_cards(
    session: Session, *, reason: str, card_ids=None, dry_run: bool = False,
) -> dict:
    """Mark every card owed exactly $0.00 as paid.

    OPERATOR RULE: a card that resolved to nothing owed is settled --
    there is no money to pay, so leaving it sitting in "owed" only makes
    the Record payout screen longer and the owed card count a lie.

    ★ NO ConsignorPayout IS CREATED, deliberately. That record asserts
    that money moved, and none did; the consignor portal shows payout
    history, so writing one $0.00 payout per card would put dozens of
    fictitious payments in front of the consignor, and this app never
    deletes a payout. The card is marked paid and the reason is recorded
    on the card's own audit trail instead -- the same shape, and the same
    undo, as the consignment amount correction beside it.

    NEVER TOUCHES AN AMOUNT. It only moves a status, so a card's frozen
    owed figure is exactly what it was before and after.

    All-or-nothing, like the correction above: every card is checked
    first and one refusal writes nothing.
    """
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ConsignmentCorrectionError("A reason is required.")

    cards = zero_owed_candidates(session, card_ids)
    planned = []
    for card in cards:
        # Paid money is never re-settled here, and a card already
        # attached to a payout belongs to that payout.
        if card.consignment_payout_id is not None:
            raise ConsignmentCorrectionError(
                f"card {card.id} is attached to payout "
                f"{card.consignment_payout_id} -- not settleable here"
            )
        planned.append({
            "inventory_card_id": card.id,
            "name": card.name,
            "consignor_id": _consignor_id_for_card(session, card),
            "amount_owed": card.consignment_amount_owed,
        })

    report = {
        "action": ZERO_SETTLEMENT_ACTION, "reason": cleaned_reason,
        "dry_run": bool(dry_run), "cards": planned, "count": len(planned),
    }
    if dry_run or not planned:
        return report

    timestamp = datetime.now(timezone.utc).isoformat()
    for plan in planned:
        card = session.get(InventoryCard, plan["inventory_card_id"])
        card.consignment_payout_status = "paid"
        session.add(InventoryChangeLog(
            actor=current_actor(),
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": ZERO_SETTLEMENT_ACTION,
                "before": {"consignment_payout_status": "owed"},
                "after": {"consignment_payout_status": "paid"},
                # recorded so the undo can prove it changed no money
                "consignment_amount_owed": plan["amount_owed"],
                "correction_reason": cleaned_reason,
                "operator_authorised": True,
                "timestamp": timestamp,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info(
        "zero-owed settlement: %s card(s) marked paid -- %s",
        len(planned), cleaned_reason,
    )
    return report


def _consignor_id_for_card(session: Session, card: InventoryCard):
    batch = session.get(Batch, card.batch_id)
    return batch.consignor_id if batch else None


def undo_zero_owed_settlement(
    session: Session, *, reason: str, correction_reason: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Put back every card a zero settlement marked paid, from its own
    audit rows. Itself audited; nothing is destroyed.

    Refuses to disturb a card that has since been attached to a real
    payout -- that is money, and it is not this undo's business.
    """
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ConsignmentCorrectionError("A reason is required to undo.")

    latest = {}
    for row in session.query(InventoryChangeLog).order_by(InventoryChangeLog.id.asc()).all():
        try:
            summary = json.loads(row.change_summary or "{}")
        except ValueError:
            logger.warning(
                "zero-settlement undo: unparseable InventoryChangeLog row %s, skipped",
                row.id,
            )
            continue
        action = summary.get("action_type")
        if action == ZERO_SETTLEMENT_ACTION:
            if correction_reason and summary.get("correction_reason") != correction_reason:
                continue
            latest[row.inventory_card_id] = summary
        elif action == ZERO_SETTLEMENT_UNDO_ACTION:
            # already put back once; it is not owed to this undo again
            latest.pop(row.inventory_card_id, None)

    planned = []
    for card_id in sorted(latest):
        card = session.get(InventoryCard, card_id)
        if card is None:
            continue
        if card.consignment_payout_id is not None:
            logger.warning(
                "zero-settlement undo: card %s now belongs to payout %s, skipped",
                card_id, card.consignment_payout_id,
            )
            continue
        planned.append({"inventory_card_id": card_id, "name": card.name})

    report = {
        "action": ZERO_SETTLEMENT_UNDO_ACTION, "reason": cleaned_reason,
        "dry_run": bool(dry_run), "cards": planned, "count": len(planned),
    }
    if dry_run or not planned:
        return report

    timestamp = datetime.now(timezone.utc).isoformat()
    for plan in planned:
        card = session.get(InventoryCard, plan["inventory_card_id"])
        card.consignment_payout_status = "owed"
        session.add(InventoryChangeLog(
            actor=current_actor(),
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": ZERO_SETTLEMENT_UNDO_ACTION,
                "before": {"consignment_payout_status": "paid"},
                "after": {"consignment_payout_status": "owed"},
                "correction_reason": cleaned_reason,
                "operator_authorised": True,
                "undoes_action": ZERO_SETTLEMENT_ACTION,
                "timestamp": timestamp,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info(
        "zero-owed settlement UNDONE: %s card(s) back to owed -- %s",
        len(planned), cleaned_reason,
    )
    return report


def undo_consignment_amount_correction(
    session: Session, reason: str, *, action: str = CONSIGNMENT_AMOUNT_CORRECTION_ACTION,
    correction_reason: str | None = None, dry_run: bool = False,
) -> dict:
    """Restore the amounts a previous correction changed, from its own
    audit rows. The undo is itself audited; nothing is destroyed.

    Reads the LATEST correction row per card, so a card corrected twice
    unwinds one step at a time rather than jumping to its oldest value.
    """
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ConsignmentCorrectionError("A reason is required to undo.")

    latest = {}
    rows = (
        session.query(InventoryChangeLog)
        .order_by(InventoryChangeLog.id.asc())
        .all()
    )
    for row in rows:
        try:
            summary = json.loads(row.change_summary or "{}")
        except ValueError:
            # A malformed audit row must never silently exclude a card
            # from its own undo.
            logger.warning(
                "consignment undo: unparseable InventoryChangeLog row %s, skipped",
                row.id,
            )
            continue
        if summary.get("action_type") != action:
            continue
        if correction_reason and summary.get("correction_reason") != correction_reason:
            continue
        latest[row.inventory_card_id] = summary

    if not latest:
        raise ConsignmentCorrectionError(
            "No matching correction found to undo.")

    planned = []
    for card_id, summary in sorted(latest.items()):
        card = session.get(InventoryCard, card_id)
        if card is None:
            continue
        planned.append({
            "inventory_card_id": card_id,
            "name": card.name,
            "before": card.consignment_amount_owed,
            "after": (summary.get("before") or {}).get("consignment_amount_owed"),
        })

    report = {
        "action": CONSIGNMENT_AMOUNT_CORRECTION_UNDO_ACTION,
        "reason": cleaned_reason, "dry_run": bool(dry_run),
        "cards": planned, "count": len(planned),
    }
    if dry_run:
        return report

    timestamp = datetime.now(timezone.utc).isoformat()
    for plan in planned:
        card = session.get(InventoryCard, plan["inventory_card_id"])
        card.consignment_amount_owed = plan["after"]
        session.add(InventoryChangeLog(
            actor=current_actor(),
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": CONSIGNMENT_AMOUNT_CORRECTION_UNDO_ACTION,
                "before": {"consignment_amount_owed": plan["before"]},
                "after": {"consignment_amount_owed": plan["after"]},
                "correction_reason": cleaned_reason,
                "operator_authorised": True,
                "undoes_action": action,
                "timestamp": timestamp,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info(
        "consignment amount correction UNDONE: %s card(s) restored -- %s",
        len(planned), cleaned_reason,
    )
    # Restoring an amount can itself land on $0.00.
    restored_to_zero = [p["inventory_card_id"] for p in planned if p["after"] == 0]
    if restored_to_zero:
        report["zero_settlement"] = settle_zero_owed_cards(
            session, reason=f"restored to $0.00 owed: {cleaned_reason}",
            card_ids=restored_to_zero,
        )
    return report


def overdue_card_ids(session, cards, *, sold_at_by_card_id, today_local=None) -> set[int]:
    """The ids of the cards `payout_is_overdue` calls overdue.

    ★ THE SAME RULE, ASKED ONCE PER CARD -- not a second rule, and not a
    query with its own idea of what past due means. The payout page's
    "Select only past-due" control, What's Owed's Amount past due column
    and the consignor portal's Overdue marker all come through
    `payout_is_overdue`, so the boxes that tick and the figure the
    operator was shown on What's Owed cannot disagree.

    `sold_at_by_card_id` is the caller's batched lookup, keyed on the
    SHIPPED allocation, because this must never issue a query per row.
    `today_local` is resolved once here and passed down, so a page cannot
    straddle midnight mid-render and mark two cards by different days.
    """
    today_local = today_local or payout_today_local()
    return {
        card.id for card in cards
        if payout_is_overdue(card, sold_at_by_card_id.get(card.id), today_local)
    }


def overdue_total(cards, overdue_ids) -> float:
    """What those cards add up to, rounded the way every other payout
    figure in this module is.

    Summed from `consignment_amount_owed`, the SAME per-card field the
    owed report and the payout rows print, so the total under the
    checkboxes and What's Owed's Amount past due are the same arithmetic
    over the same numbers.
    """
    return round(sum(
        card.consignment_amount_owed or 0
        for card in cards if card.id in overdue_ids
    ), 2)
