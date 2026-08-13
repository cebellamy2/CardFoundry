"""Shared, auditable pricing hierarchy for previews and initial listings."""

import hashlib
import json


ALL_PRICING_LANGUAGES = [
    "EN", "JA", "FR", "IT", "DE", "ES", "AR", "CS", "CT", "EL",
    "HE", "KO", "LA", "PH", "PT", "RU", "SA", "QYA",
]


def evidence_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def competitor_decision(competitor: dict, undercut_cents: int, floor_cents: int) -> dict:
    price = int(competitor["price_cents"])
    raw = price - int(undercut_cents)
    target = max(raw, int(floor_cents))
    result = {
        "status": "priced",
        "classification": (
            "floor_limited_competitor" if target != raw else "competitor_undercut"
        ),
        "price_source": "competitor",
        "target_price_cents": target,
        "floor_applied": target != raw,
        "competitor": competitor,
        "market": None,
    }
    result["evidence_hash"] = evidence_hash(result)
    return result


def market_decision(market: dict | None, floor_cents: int) -> dict:
    if not market or not isinstance(market.get("price_cents"), (int, float)):
        result = {
            "status": "hold", "classification": "hold_no_price_evidence",
            "price_source": None, "target_price_cents": None,
            "floor_applied": False, "competitor": None, "market": market,
        }
    else:
        price = int(market["price_cents"])
        target = max(price, int(floor_cents))
        result = {
            "status": "priced",
            "classification": "floor_limited_market" if target != price else "market_price_fallback",
            "price_source": "market", "target_price_cents": target,
            "floor_applied": target != price, "competitor": None, "market": market,
        }
    result["evidence_hash"] = evidence_hash(result)
    return result


def existing_floor_decision(current_price_cents: int, floor_cents: int) -> dict:
    """Apply the owner's absolute floor even when no market signal exists."""
    current = int(current_price_cents)
    floor = int(floor_cents)
    if current >= floor:
        result = {
            "status": "preserve", "classification": "existing_price_preserved",
            "price_source": "existing_history", "target_price_cents": current,
            "floor_applied": False,
        }
    else:
        result = {
            "status": "priced", "classification": "floor_corrected_existing",
            "price_source": "owner_floor_policy", "target_price_cents": floor,
            "floor_applied": True,
        }
    result["evidence_hash"] = evidence_hash(result)
    return result


def market_evidence_from_catalog(identity: dict, catalog_response: dict) -> dict | None:
    """Accept only an exact printing and finish-specific Mana Pool market value."""
    expected_scryfall = str(identity.get("catalog_scryfall_id") or identity.get("scryfall_id") or "").lower()
    matches = []
    for product in catalog_response.get("data") or []:
        if (
            str(product.get("name") or "").casefold() == str(identity.get("name") or "").casefold()
            and str(product.get("set_code") or "").upper() == str(identity.get("set_code") or "").upper()
            and str(product.get("number") or "").upper() == str(identity.get("collector_number") or "").upper()
            and (not expected_scryfall or str(product.get("scryfall_id") or "").lower() == expected_scryfall)
        ):
            matches.append(product)
    if len(matches) != 1:
        return None
    finish = str(identity.get("finish_id") or "").upper()
    field = {"NF": "price_market", "FO": "price_market_foil"}.get(finish)
    if not field:
        return None
    value = matches[0].get(field)
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    # Mana Pool catalog price fields are integer cents (like price_cents).
    return {
        "price_cents": int(value),
        "source_endpoint": "/products/singles",
        "source_field": field,
        "effective_as_of": (catalog_response.get("meta") or {}).get("as_of"),
        "scryfall_id": matches[0].get("scryfall_id"),
        "set_code": matches[0].get("set_code"),
        "collector_number": matches[0].get("number"),
        "finish_id": finish,
        "language_specific": False,
        "condition_specific": False,
    }
