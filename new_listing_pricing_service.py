"""Read-only initial competitive pricing for validated net-new product bindings."""

import json

from pricing_diagnostic_service import eligible_competitor_conditions, single_details


def _text(value) -> str:
    return str(value or "").strip()


def request_from_binding(binding) -> dict:
    identity = json.loads(binding.requested_identity_json)
    allowed = eligible_competitor_conditions(identity["condition_id"])
    return {
        "binding_id": binding.id,
        "product_id": binding.product_id,
        "identity": identity,
        "allowed_conditions": allowed,
        "cart_item": {
            "type": "mtg_single",
            "name": identity["name"],
            "set_code": identity["set_code"],
            "collector_number": identity["collector_number"],
            "language_ids": [identity["language_id"]],
            "finish_ids": [identity["finish_id"]],
            "condition_ids": allowed,
            "quantity_requested": 1,
        },
    }


def listing_matches_request(listing: dict, request: dict) -> bool:
    single = single_details(listing)
    identity = request["identity"]
    return (
        _text(single.get("scryfall_id")).lower() == identity["scryfall_id"].lower()
        and _text(single.get("name")).casefold() == identity["name"].casefold()
        and _text(single.get("set")).upper() == identity["set_code"].upper()
        and _text(single.get("number")).upper() == identity["collector_number"].upper()
        and _text(single.get("language_id")).upper() == identity["language_id"].upper()
        and _text(single.get("finish_id")).upper() == identity["finish_id"].upper()
        and _text(single.get("condition_id")).upper() in request["allowed_conditions"]
    )


def price_initial_bindings(
    bindings, optimizer_call, listings_call, seller_id,
    batch_size=20, undercut_cents=5, floor_cents=65,
) -> dict:
    """Verify competitor prices without seller inventory or any write calls."""
    requests = [request_from_binding(binding) for binding in bindings]
    requests.sort(key=lambda row: row["binding_id"])
    selected_ids = []
    holds = {}
    calls = retries = 0
    for start in range(0, len(requests), batch_size):
        remaining = requests[start:start + batch_size]
        while remaining:
            calls += 1
            response = optimizer_call([row["cart_item"] for row in remaining], seller_id)
            conflicts = response.get("_conflicts") or []
            if not conflicts:
                selected_ids.extend(
                    _text(row.get("inventory_id"))
                    for row in response.get("cart") or [] if row.get("inventory_id")
                )
                break
            indexes = {
                detail.get("item", {}).get("index") for detail in conflicts
                if isinstance(detail.get("item", {}).get("index"), int)
            }
            if not indexes or any(index < 0 or index >= len(remaining) for index in indexes):
                for request in remaining:
                    holds[request["binding_id"]] = "Optimizer conflict could not be mapped"
                break
            next_remaining = []
            for index, request in enumerate(remaining):
                if index in indexes:
                    holds[request["binding_id"]] = "No seller-excluded competitor satisfies this request"
                else:
                    next_remaining.append(request)
            remaining = next_remaining
            if remaining:
                retries += 1

    unique_ids = list(dict.fromkeys(selected_ids))
    listings = listings_call(unique_ids) if unique_ids else []
    listing_by_id = {_text(item.get("id")): item for item in listings if item.get("id")}
    results = []
    assigned = set()
    for request in requests:
        reason = holds.get(request["binding_id"])
        matches = [] if reason else [
            listing for inventory_id, listing in listing_by_id.items()
            if inventory_id not in assigned and listing_matches_request(listing, request)
        ]
        if not reason and len(matches) != 1:
            reason = "No exact resolved competitor matched" if not matches else "Ambiguous competitor mapping"
        if reason:
            results.append({
                "binding_id": request["binding_id"], "product_id": request["product_id"],
                "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                "status": "hold", "reason": reason, "target_price_cents": None,
                "competitor_inventory_id": None, "competitor_price_cents": None,
            })
            continue
        listing = matches[0]
        assigned.add(_text(listing.get("id")))
        price = listing.get("price_cents")
        if not isinstance(price, (int, float)) or int(price) < 1 or int(listing.get("quantity") or 0) < 1:
            results.append({
                "binding_id": request["binding_id"], "product_id": request["product_id"],
                "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                "status": "hold", "reason": "Competitor listing is stale or has no valid price",
                "target_price_cents": None, "competitor_inventory_id": None,
                "competitor_price_cents": None,
            })
            continue
        results.append({
            "binding_id": request["binding_id"], "product_id": request["product_id"],
            "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
            "status": "priced", "reason": "Exact seller-excluded competitor validated",
            "target_price_cents": max(int(price) - undercut_cents, floor_cents),
            "competitor_inventory_id": _text(listing.get("id")),
            "competitor_price_cents": int(price),
            "competitor_condition_id": _text(single_details(listing).get("condition_id")).upper(),
        })
    return {
        "preview_only": True,
        "results": results,
        "summary": {
            "bindings": len(bindings), "priced": sum(r["status"] == "priced" for r in results),
            "held": sum(r["status"] == "hold" for r in results),
            "optimizer_calls": calls, "optimizer_retries": retries,
            "listing_ids_resolved": len(unique_ids),
        },
    }
