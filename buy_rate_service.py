"""Buy-offer tier resolution for the buylist/offer workflow (CF-BUY-001).

Mirrors consignment_service.py's own settings pattern exactly: one JSON
blob in AppSetting, a pure resolver function, a default fallback used
whenever no operator-saved value exists yet. Buy rates are a genuinely
separate table from consignment payout tiers (resolve_consignment_payout,
consignment_service.py) even though they're structurally similar --
buying pays the SELLER an amount now, consigning pays the CONSIGNOR a
cut of a FUTURE sale, and the two tables' actual numbers differ (70% buy
vs 80% consign over $5, $0.00 buy vs $0.10 consign under $1) precisely
because the risk/timing is different. Consignment's own tiers are read
here read-only, for the admin panel's side-by-side comparison only --
never merged or shared.
"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from models import AppSetting


BUY_RATE_SETTINGS_KEY = "buylist_buy_rate_settings"

# Ordered narrowest-to-widest, same convention as DEFAULT_CONSIGNMENT_
# TIERS -- the first tier whose max_price the card's LP+ value doesn't
# exceed applies; max_price=None is the catch-all top band. Operator's
# own numbers (2026-09-07): under $1 pays nothing at all ("freebies for
# scanning their cards"), not a token amount and not folded into a
# separate "bulk" concept -- there isn't one for buying.
DEFAULT_BUY_RATE_SETTINGS = {
    "tiers": [
        {"max_price": 0.99, "type": "flat", "value": 0.00},
        {"max_price": 2.99, "type": "percent", "value": 0.60},
        {"max_price": 4.99, "type": "percent", "value": 0.65},
        {"max_price": None, "type": "percent", "value": 0.70},
    ],
    # Seller (non-owned) piles pre-suggest consignment above this LP+
    # value -- a suggestion the operator can still uncheck per card, not
    # a hard rule (see the buylist investigation report, decision 3).
    "consignment_suggest_threshold": 5.00,
    # CF-BUY-002/003 will read this to decide how a card whose SCANNED
    # condition is below LP gets priced: from that condition's own Mana
    # Pool variants[].low_price instead of the LP+ figure, guarded to
    # never exceed LP+, flagged when that condition has very few
    # listings to price from. Not implemented by this ticket -- recorded
    # now so the settings shape is already there when it is.
    "below_lp_pricing": "condition_variant",
}


def get_buy_rate_settings(session: Session) -> dict:
    setting = session.query(AppSetting).filter(
        AppSetting.key == BUY_RATE_SETTINGS_KEY,
    ).first()
    if not setting or not setting.value:
        return DEFAULT_BUY_RATE_SETTINGS
    return json.loads(setting.value)


def validate_buy_rate_settings(settings: dict) -> None:
    """Raises ValueError with a specific, operator-facing reason on
    anything malformed. Called by set_buy_rate_settings before anything
    is ever persisted -- a bad tier table is refused, not saved and
    left to break resolve_buy_offer later. Deliberately generic (never
    assumes exactly 4 tiers, or that only the first is flat) even though
    the admin panel's own form only exposes that one fixed shape for
    editing -- the same validation must hold for any future tier-count
    change without itself needing to change.
    """
    tiers = settings.get("tiers")
    if not tiers or not isinstance(tiers, list):
        raise ValueError("At least one tier is required.")
    previous_max = None
    for index, tier in enumerate(tiers):
        position = index + 1
        max_price = tier.get("max_price")
        tier_type = tier.get("type")
        value = tier.get("value")
        if tier_type not in ("flat", "percent"):
            raise ValueError(f'Tier {position}: type must be "flat" or "percent".')
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Tier {position}: value must be a non-negative number.")
        if tier_type == "percent" and value > 1:
            raise ValueError(f"Tier {position}: a percent value must be between 0 and 1.")
        is_last = index == len(tiers) - 1
        if is_last:
            if max_price is not None:
                raise ValueError("The last tier must be the catch-all band (max_price null).")
        else:
            if max_price is None:
                raise ValueError(f"Tier {position}: only the LAST tier may have a null max_price.")
            if not isinstance(max_price, (int, float)) or isinstance(max_price, bool):
                raise ValueError(f"Tier {position}: max_price must be a number.")
            if previous_max is not None and max_price <= previous_max:
                raise ValueError(
                    f"Tier {position}: max_price (${max_price:.2f}) must be greater than "
                    f"the previous tier's (${previous_max:.2f})."
                )
            previous_max = max_price
    threshold = settings.get("consignment_suggest_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold < 0:
        raise ValueError("Consignment suggestion threshold must be a non-negative number.")
    if settings.get("below_lp_pricing") not in ("condition_variant",):
        raise ValueError('below_lp_pricing must be "condition_variant".')


def set_buy_rate_settings(session: Session, settings: dict) -> None:
    validate_buy_rate_settings(settings)
    value = json.dumps(settings)
    setting = session.query(AppSetting).filter(
        AppSetting.key == BUY_RATE_SETTINGS_KEY,
    ).first()
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=BUY_RATE_SETTINGS_KEY, value=value))


def resolve_buy_offer(settings: dict, lp_plus_cents: int | None) -> dict:
    """Pure resolver, same style as resolve_consignment_payout (first
    matching tier wins) but a richer dict return -- tier_index/type/
    value alongside offer_cents, so a caller (the buylist report) can
    show which tier/rate actually applied, not just the dollar result.

    lp_plus_cents is None whenever a card has no LP+ at all (Mana Pool
    returned no listings for that printing -- confirmed live in the
    buylist investigation: price_cents_lp_plus comes back JSON null,
    not zero). No offer can be computed for that; flagged rather than
    silently defaulting to a $0 tier, which would be indistinguishable
    from a genuinely-priced under-$1 card.

    All arithmetic is done in integer cents throughout (tier max_price/
    value are stored in dollars in settings, converted here) rather than
    round-tripping lp_plus_cents through float dollars and back -- the
    boundary tests ($0.99 vs $1.00 etc.) need exact cent comparisons,
    not float-dollar ones.
    """
    if lp_plus_cents is None:
        return {"tier_index": None, "type": None, "value": None, "offer_cents": None, "flagged": True}
    for index, tier in enumerate(settings["tiers"]):
        max_price = tier["max_price"]
        max_price_cents = round(max_price * 100) if max_price is not None else None
        if max_price_cents is None or lp_plus_cents <= max_price_cents:
            if tier["type"] == "flat":
                offer_cents = round(tier["value"] * 100)
            else:
                offer_cents = round(lp_plus_cents * tier["value"])
            return {
                "tier_index": index, "type": tier["type"], "value": tier["value"],
                "offer_cents": offer_cents, "flagged": False,
            }
    raise ValueError("Buy rate tier table has no catch-all band")
