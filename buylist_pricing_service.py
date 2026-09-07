"""CF-BUY-003: LP+ (or below-LP condition-variant) pricing for buylist
pile lines.

This is a NEW, separate read of Mana Pool's /products/singles catalog --
pricing_decision_service.market_evidence_from_catalog already reads that
same endpoint for the live new-listing pipeline, but exclusively the
price_market/price_market_foil fields. Those are never touched here.
Buylist reads a different field family entirely (price_cents_lp_plus and
its foil/etched siblings, plus variants[].low_price for below-LP condition
pricing) -- two genuinely different questions asked of the same catalog
response, not a shared computation.

Mirrors the simple chunked-by-100 catalog-call pattern every other caller
of get_single_catalog_by_scryfall_ids already uses (main.py's
_chute_review_html, production_rebuild_rehearsal.py) -- not
new_listing_pricing_service.py's much heavier competitor-optimizer
batching, which buylist pricing has no need for (no competitor exclusion,
no retries/conflicts, just a straight catalog read).
"""

from datetime import datetime

from import_service import normalized_condition_id, normalized_finish_id
from pricing_diagnostic_service import CONDITION_ORDER

# Operator decision (CF-BUY-003, 2026-09-07): a below-LP condition price
# backed by fewer than this many active listings is priced anyway (it's
# still the best real signal available) but flagged for manual review
# rather than trusted silently.
MIN_LISTINGS_FOR_CONFIDENCE = 3

_LP_PLUS_FIELD_BY_FINISH = {
    "NF": "price_cents_lp_plus",
    "FO": "price_cents_lp_plus_foil",
    "EF": "price_cents_lp_plus_etched",
}


def fetch_catalog_products(scryfall_ids, catalog_lookup) -> dict:
    """Batched (chunked by 100) /products/singles read, keyed by
    lowercased scryfall_id. `catalog_lookup` is
    manapool_service.get_single_catalog_by_scryfall_ids (or a test
    double with the same signature) -- injected rather than imported
    directly so tests never need a real Mana Pool call."""
    unique_ids = list(dict.fromkeys(str(s).lower() for s in scryfall_ids if s))
    products_by_id: dict[str, dict] = {}
    for start in range(0, len(unique_ids), 100):
        chunk = unique_ids[start:start + 100]
        response = catalog_lookup(chunk) or {}
        for product in response.get("data") or []:
            scryfall_id = str(product.get("scryfall_id") or "").lower()
            if scryfall_id:
                products_by_id[scryfall_id] = product
    return products_by_id


def resolve_pile_line_price(product: dict | None, condition: str, finish: str, language: str | None = None) -> dict:
    """Pure resolver: given one Mana Pool catalog product (or None, when
    the catalog returned nothing for this printing) and the pile line's
    own scanned condition/finish/language, returns
    {"price_cents", "price_basis", "price_flagged"}.

    price_basis is one of:
      - "lp_plus": condition is LP or better, priced from the matching
        LP+ family field directly.
      - "condition_variant": condition is worse than LP, priced from that
        exact condition+finish's own variants[].low_price.
      - "condition_variant_clamped": as above, but the variant price
        exceeded LP+ (the known Mana Pool data-quality issue -- a single
        outlier listing, e.g. $115,241 against a 62-cent LP+ figure) and
        was clamped down to it. Always flagged.
      - "lp_plus_fallback": condition is worse than LP but no matching
        variant exists at all; falls back to the LP+ figure. Always
        flagged, since pricing a played card at the full LP+ figure is
        itself a fallback, not a real read.
      - None: no usable price found anywhere (no LP+, no variant).

    price_cents is None only when price_basis is also None -- every other
    basis carries a real (possibly flagged) number.
    """
    finish_id = normalized_finish_id(finish) or "NF"
    condition_id = normalized_condition_id(condition) or "LP"
    language_id = str(language).strip().upper() if language else "EN"

    lp_field = _LP_PLUS_FIELD_BY_FINISH.get(finish_id, "price_cents_lp_plus")
    lp_plus_raw = (product or {}).get(lp_field)
    lp_plus_cents = int(lp_plus_raw) if isinstance(lp_plus_raw, (int, float)) else None

    is_lp_or_better = (
        condition_id not in CONDITION_ORDER
        or CONDITION_ORDER.index(condition_id) <= CONDITION_ORDER.index("LP")
    )
    if is_lp_or_better:
        return {
            "price_cents": lp_plus_cents,
            "price_basis": "lp_plus" if lp_plus_cents is not None else None,
            "price_flagged": lp_plus_cents is None,
        }

    matching_variant = None
    for variant in (product or {}).get("variants") or []:
        if (
            str(variant.get("condition_id") or "").upper() == condition_id
            and str(variant.get("finish_id") or "").upper() == finish_id
            and str(variant.get("language_id") or "EN").upper() == language_id
        ):
            matching_variant = variant
            break

    if matching_variant is None:
        return {
            "price_cents": lp_plus_cents,
            "price_basis": "lp_plus_fallback" if lp_plus_cents is not None else None,
            "price_flagged": True,
        }

    try:
        variant_cents = int(matching_variant.get("low_price") or 0)
    except (TypeError, ValueError):
        variant_cents = 0
    try:
        listing_count = int(matching_variant.get("available_quantity") or 0)
    except (TypeError, ValueError):
        listing_count = 0

    if variant_cents <= 0:
        return {
            "price_cents": lp_plus_cents,
            "price_basis": "lp_plus_fallback" if lp_plus_cents is not None else None,
            "price_flagged": True,
        }

    if lp_plus_cents is not None and variant_cents > lp_plus_cents:
        return {"price_cents": lp_plus_cents, "price_basis": "condition_variant_clamped", "price_flagged": True}

    return {
        "price_cents": variant_cents,
        "price_basis": "condition_variant",
        "price_flagged": listing_count < MIN_LISTINGS_FOR_CONFIDENCE,
    }


def price_pending_pile_line(line, product: dict | None, buy_settings: dict, *, is_owned: bool, now=None) -> None:
    """Mutates `line` in place: price_cents/price_basis/price_flagged/
    price_as_of, tier_index/offer_cents (the buy-side calculation --
    always computed and always locked, regardless of the line's eventual
    disposition, since line_status can be toggled later on the report),
    and -- for a seller pile only -- the consignment auto-suggestion on
    line_status when the resolved price is over the configured threshold.
    Caller commits.
    """
    from buy_rate_service import resolve_buy_offer

    now = now or datetime.now()
    priced = resolve_pile_line_price(product, line.condition, line.finish, language=line.language)
    line.price_cents = priced["price_cents"]
    line.price_basis = priced["price_basis"]
    line.price_flagged = priced["price_flagged"]
    line.price_as_of = now

    offer = resolve_buy_offer(buy_settings, line.price_cents)
    line.tier_index = offer["tier_index"]
    line.offer_cents = offer["offer_cents"]

    if not is_owned and line.price_cents is not None:
        threshold_cents = round(buy_settings["consignment_suggest_threshold"] * 100)
        if line.price_cents > threshold_cents:
            line.line_status = "consignment"
