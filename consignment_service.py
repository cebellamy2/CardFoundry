"""Consignment payout tier resolution and the operator-facing owed report.

CardFoundry takes some cards on consignment rather than buying them
outright -- a cut of the sale price is owed to the consignor once (and
only once) the card actually ships. Consignment status lives on the
Batch, not the card: every InventoryCard in a consignment batch belongs
to that batch's consignor. The payout itself is a shop-wide, price-tiered
table (not a rate negotiated per consignor or per card), resolved against
the card's actual sale price -- never the estimated value captured at
intake -- so the tiers exist specifically to avoid overpaying against a
presale-hype estimate that didn't hold up.
"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from models import AppSetting, Batch, Consignor, InventoryCard


CONSIGNMENT_TIERS_SETTING_KEY = "consignment_payout_tiers"

# Ordered narrowest-to-widest; the first tier whose max_price the sale
# price doesn't exceed applies. max_price=None is the catch-all top band.
DEFAULT_CONSIGNMENT_TIERS = [
    {"max_price": 1.00, "type": "flat", "value": 0.10},
    {"max_price": 2.99, "type": "percent", "value": 0.60},
    {"max_price": 4.99, "type": "percent", "value": 0.65},
    {"max_price": None, "type": "percent", "value": 0.80},
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
            return round(sale_price * tier["value"], 2)
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
