"""Read-only initial competitive pricing for validated net-new product bindings."""

import json
from pricing_diagnostic_service import eligible_competitor_conditions, single_details
from pricing_decision_service import (
    ALL_PRICING_LANGUAGES, competitor_decision, market_decision,
    market_evidence_from_catalog,
)
from manual_price_override_service import valid_override_for_binding


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
            "language_ids": list(ALL_PRICING_LANGUAGES),
            "finish_ids": [identity["finish_id"]],
            "condition_ids": allowed,
            "quantity_requested": 1,
        },
    }


def listing_matches_request(listing: dict, request: dict) -> bool:
    single = single_details(listing)
    identity = request["identity"]
    return (
        _text(single.get("scryfall_id")).lower() in {
            _text(identity.get("scryfall_id")).lower(),
            _text(identity.get("catalog_scryfall_id")).lower(),
        }
        and _text(single.get("name")).casefold() == identity["name"].casefold()
        and _text(single.get("set")).upper() == identity["set_code"].upper()
        and _text(single.get("number")).upper() == identity["collector_number"].upper()
        and _text(single.get("finish_id")).upper() == identity["finish_id"].upper()
        and _text(single.get("condition_id")).upper() in request["allowed_conditions"]
    )


def price_initial_bindings(
    bindings, optimizer_call, listings_call, seller_id,
    batch_size=20, undercut_cents=5, floor_cents=65, market_catalog_call=None,
    manual_overrides=(),
) -> dict:
    """Verify competitor prices without seller inventory or any write calls."""
    binding_by_id = {binding.id: binding for binding in bindings}
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
        if reason == "No seller-excluded competitor satisfies this request" and market_catalog_call:
            payload = market_catalog_call([request["product_id"]])
            market = market_evidence_from_catalog(request["identity"], payload)
            decision = market_decision(market, floor_cents)
            if decision["status"] == "priced":
                results.append({
                    "binding_id": request["binding_id"], "product_id": request["product_id"],
                    "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                    "status": "priced", "reason": "Trustworthy exact-printing market fallback",
                    "target_price_cents": decision["target_price_cents"],
                    "price_classification": decision["classification"], "price_source": "market",
                    "floor_applied": decision["floor_applied"], "market_evidence": market,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "evidence_hash": decision["evidence_hash"],
                })
                continue
        if reason == "No seller-excluded competitor satisfies this request":
            override = valid_override_for_binding(
                binding_by_id[request["binding_id"]], manual_overrides, floor_cents,
            )
            if override:
                manual_evidence = {
                    "source_classification": "manual_price_override",
                    "manual_override_evidence_hash": override.evidence_hash,
                    "binding_evidence_hash": override.binding_evidence_hash,
                    "product_id": request["product_id"], "identity": request["identity"],
                    "manual_price_cents": override.manual_price_cents,
                    "note": override.note, "pricing_floor_cents": floor_cents,
                    "automatic_competitor_status": "unavailable",
                    "automatic_market_status": "unavailable",
                }
                from pricing_decision_service import evidence_hash
                results.append({
                    "binding_id": request["binding_id"], "product_id": request["product_id"],
                    "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                    "status": "priced", "reason": "Reviewed manual fallback after current automatic HOLD",
                    "target_price_cents": override.manual_price_cents,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "manual_price_override", "price_source": "manual",
                    "floor_applied": False, "manual_evidence": manual_evidence,
                    "evidence_hash": evidence_hash(manual_evidence),
                })
                continue
        if reason:
            results.append({
                "binding_id": request["binding_id"], "product_id": request["product_id"],
                "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                "status": "hold", "reason": reason, "target_price_cents": None,
                "competitor_inventory_id": None, "competitor_price_cents": None,
                "competitor_effective_as_of": None,
                "price_classification": "hold_no_price_evidence", "price_source": None,
            })
            continue
        listing = matches[0]
        assigned.add(_text(listing.get("id")))
        price = listing.get("price_cents")
        if (
            not isinstance(price, (int, float)) or int(price) < 1
            or int(listing.get("quantity") or 0) < 1
            or not listing.get("effective_as_of")
        ):
            results.append({
                "binding_id": request["binding_id"], "product_id": request["product_id"],
                "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                "status": "hold", "reason": "Competitor listing is stale or has no valid price",
                "target_price_cents": None, "competitor_inventory_id": None,
                "competitor_price_cents": None,
                "competitor_effective_as_of": None,
            })
            continue
        single = single_details(listing)
        decision = competitor_decision({
            "inventory_id": _text(listing.get("id")), "product_id": _text(listing.get("product_id")),
            "seller_id": listing.get("seller_id"),
            "language_id": _text(single.get("language_id")).upper(),
            "condition_id": _text(single.get("condition_id")).upper(),
            "finish_id": _text(single.get("finish_id")).upper(),
            "price_cents": int(price), "effective_as_of": listing.get("effective_as_of"),
        }, undercut_cents, floor_cents)
        results.append({
            "binding_id": request["binding_id"], "product_id": request["product_id"],
            "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
            "status": "priced", "reason": "Exact seller-excluded competitor validated",
            "target_price_cents": decision["target_price_cents"],
            "competitor_inventory_id": _text(listing.get("id")),
            "competitor_price_cents": int(price),
            "competitor_condition_id": _text(single_details(listing).get("condition_id")).upper(),
            "competitor_language_id": _text(single_details(listing).get("language_id")).upper(),
            "competitor_finish_id": _text(single_details(listing).get("finish_id")).upper(),
            "competitor_effective_as_of": listing.get("effective_as_of"),
            "price_classification": decision["classification"], "price_source": "competitor",
            "floor_applied": decision["floor_applied"], "evidence_hash": decision["evidence_hash"],
        })
    for row in results:
        evidence = {
            "binding_id": row["binding_id"], "product_id": row["product_id"],
            "identity": row["identity"], "allowed_conditions": row["allowed_conditions"],
            "status": row["status"], "reason": row["reason"],
            "target_price_cents": row["target_price_cents"],
            "competitor_inventory_id": row["competitor_inventory_id"],
            "competitor_price_cents": row["competitor_price_cents"],
            "competitor_condition_id": row.get("competitor_condition_id"),
            "competitor_effective_as_of": row.get("competitor_effective_as_of"),
        }
        if not row.get("evidence_hash"):
            from pricing_decision_service import evidence_hash
            row["evidence_hash"] = evidence_hash(evidence)
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
