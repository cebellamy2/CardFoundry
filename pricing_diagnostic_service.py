"""Pure validation helpers for the read-only two-card pricing proof."""


CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DMG"]
from pricing_decision_service import ALL_PRICING_LANGUAGES


def eligible_competitor_conditions(condition_id: str) -> list[str]:
    condition = str(condition_id or "").strip().upper()
    if condition not in CONDITION_ORDER:
        return [condition] if condition else []
    return CONDITION_ORDER[:CONDITION_ORDER.index(condition) + 1]


def single_details(item: dict) -> dict:
    return ((item.get("product") or {}).get("single") or {})


def exact_identity(item: dict) -> tuple[str, str, str, str, str]:
    single = single_details(item)
    return (
        str(single.get("set") or "").strip().upper(),
        str(single.get("number") or "").strip().upper(),
        str(single.get("language_id") or "").strip().upper(),
        str(single.get("finish_id") or "").strip().upper(),
        str(single.get("mtgjson_id") or "").strip(),
    )


def pricing_identity(item: dict) -> tuple[str, str, str, str]:
    """Pricing identity deliberately excludes language and condition."""
    single = single_details(item)
    return (
        str(single.get("set") or "").strip().upper(),
        str(single.get("number") or "").strip().upper(),
        str(single.get("finish_id") or "").strip().upper(),
        str(single.get("scryfall_id") or "").strip().lower(),
    )


def build_exact_request(item: dict) -> dict:
    single = single_details(item)
    mtgjson_id = str(single.get("mtgjson_id") or "").strip()
    if not mtgjson_id:
        raise ValueError(f"{single.get('name') or 'Card'} has no mtgjson_id.")

    allowed = eligible_competitor_conditions(single.get("condition_id"))
    if not allowed:
        raise ValueError(f"{single.get('name') or 'Card'} has no valid condition.")

    return {
        "source_inventory_id": str(item.get("id") or ""),
        "identity": exact_identity(item),
        "product_id": str(item.get("product_id") or ""),
        "name": str(single.get("name") or "Unknown card"),
        "set_code": str(single.get("set") or ""),
        "collector_number": str(single.get("number") or ""),
        "language_id": str(single.get("language_id") or "").upper(),
        "finish_id": str(single.get("finish_id") or "").upper(),
        "condition_id": str(single.get("condition_id") or "").upper(),
        "allowed_conditions": allowed,
        "cart_item": {
            "type": "mtg_single",
            "name": str(single.get("name") or ""),
            "mtgjson_id": mtgjson_id,
            "language_ids": [str(single.get("language_id") or "").upper()],
            "finish_ids": [str(single.get("finish_id") or "").upper()],
            "condition_ids": allowed,
            "quantity_requested": 1,
        },
    }


def build_pricing_request(item: dict) -> dict:
    """Build the all-language request used only for price discovery."""
    request = build_exact_request(item)
    single = single_details(item)
    request["identity"] = pricing_identity(item)
    request["cart_item"].pop("mtgjson_id", None)
    request["cart_item"].update({
        "set_code": str(single.get("set") or ""),
        "collector_number": str(single.get("number") or ""),
        "language_ids": list(ALL_PRICING_LANGUAGES),
    })
    return request


def map_optimizer_listings(
    requests: list[dict],
    optimizer_response: dict,
    listings: list[dict],
    undercut_cents: int = 5,
    floor_cents: int = 65,
) -> dict:
    selected = optimizer_response.get("cart") or []
    selected_by_id = {}
    errors = []

    for row in selected:
        inventory_id = str(row.get("inventory_id") or "")
        quantity = int(row.get("quantity_selected") or 0)
        if not inventory_id or quantity != 1 or inventory_id in selected_by_id:
            errors.append("Optimizer returned an invalid or duplicate selection.")
            continue
        selected_by_id[inventory_id] = row

    listing_by_id = {
        str(item.get("id") or ""): item
        for item in listings
        if item.get("id")
    }
    missing = sorted(set(selected_by_id) - set(listing_by_id))
    extra = sorted(set(listing_by_id) - set(selected_by_id))
    if missing:
        errors.append("Selected inventory IDs were not resolved: " + ", ".join(missing))
    if extra:
        errors.append("Listing response contained unrequested IDs: " + ", ".join(extra))

    results = []
    assigned_ids = set()
    for request in requests:
        candidates = []
        for inventory_id in selected_by_id:
            listing = listing_by_id.get(inventory_id)
            if listing and exact_identity(listing) == request["identity"]:
                candidates.append(listing)

        result = {
            "requested": {
                "name": request["name"],
                "set_code": request["set_code"],
                "collector_number": request["collector_number"],
                "language_id": request["language_id"],
                "finish_id": request["finish_id"],
                "condition_id": request["condition_id"],
                "product_id": request["product_id"],
            },
            "allowed_conditions": request["allowed_conditions"],
            "inventory_id": None,
            "resolved_identity": None,
            "resolved_condition": None,
            "price_cents": None,
            "target_cents": None,
            "passed": False,
            "verdict": "HOLD",
            "reason": None,
        }

        if len(candidates) != 1:
            result["reason"] = (
                "No exact resolved listing matched the request."
                if not candidates
                else "Multiple resolved listings matched the request."
            )
            results.append(result)
            continue

        listing = candidates[0]
        inventory_id = str(listing.get("id") or "")
        single = single_details(listing)
        condition = str(single.get("condition_id") or "").upper()
        price = listing.get("price_cents")
        result.update({
            "inventory_id": inventory_id,
            "resolved_identity": {
                "name": single.get("name"),
                "set_code": single.get("set"),
                "collector_number": single.get("number"),
                "language_id": single.get("language_id"),
                "finish_id": single.get("finish_id"),
                "product_id": listing.get("product_id"),
                "mtgjson_id": single.get("mtgjson_id"),
            },
            "resolved_condition": condition,
            "price_cents": price,
        })
        assigned_ids.add(inventory_id)

        if condition not in request["allowed_conditions"]:
            result["reason"] = "Resolved listing has a worse or unknown condition."
        elif not isinstance(price, (int, float)) or int(price) < 1:
            result["reason"] = "Resolved listing has no valid price_cents."
        else:
            result["price_cents"] = int(price)
            result["target_cents"] = max(
                int(price) - int(undercut_cents),
                int(floor_cents),
            )
            result["passed"] = True
            result["verdict"] = "PASS"
        results.append(result)

    unassigned = sorted(set(selected_by_id) - assigned_ids)
    if unassigned:
        errors.append("Selected listings could not be assigned: " + ", ".join(unassigned))

    urza = next(
        (row for row in results if row["requested"]["name"] == "Urza's Ruinous Blast"),
        None,
    )
    if (
        urza
        and urza.get("passed")
        and (urza.get("price_cents"), urza.get("target_cents")) != (500, 495)
    ):
        urza["passed"] = False
        urza["verdict"] = "HOLD"
        urza["reason"] = "Urza regression check expected competitor 500 and target 495."

    passed = not errors and len(results) == 2 and all(row["passed"] for row in results)
    return {
        "passed": passed,
        "verdict": "PASS" if passed else "HOLD",
        "errors": errors,
        "results": results,
    }


def run_two_card_proof(
    seller_items: list[dict],
    seller_id: str,
    optimizer_call,
    listings_call,
) -> dict:
    if len(seller_items) != 2:
        raise ValueError("The diagnostic requires exactly two seller items.")

    requests = [build_exact_request(item) for item in seller_items]
    if requests[0]["identity"] == requests[1]["identity"]:
        raise ValueError("Diagnostic requests must use distinct exact printings.")

    optimized = optimizer_call(
        [request["cart_item"] for request in requests],
        seller_id,
    )
    inventory_ids = [
        str(row.get("inventory_id") or "")
        for row in optimized.get("cart") or []
        if row.get("inventory_id")
    ]
    listings = listings_call(inventory_ids)
    return map_optimizer_listings(requests, optimized, listings)
