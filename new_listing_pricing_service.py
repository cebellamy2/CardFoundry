"""Read-only initial competitive pricing for validated net-new product bindings."""

import json

import competitor_pricing_service
from competitor_pricing_service import _RequestPacer
from pricing_diagnostic_service import eligible_competitor_conditions, single_details
from pricing_decision_service import (
    ALL_PRICING_LANGUAGES, competitor_decision, market_decision,
    market_evidence_from_catalog,
)
from manual_price_override_service import valid_override_for_binding, valid_override_for_identity


def _text(value) -> str:
    return str(value or "").strip()


def _pacer(min_request_interval: float | None) -> _RequestPacer:
    """Same pacing mechanism and the same shared interval Flow B pricing
    uses (competitor_pricing_service.OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS)
    -- both call the identical rate-limited /buyer/optimizer endpoint, so
    they share one budget. Read at call time, not as a default argument,
    so the module constant stays overridable (tests set it to 0 via the
    suite-wide autouse fixture; a manual run can raise it).

    This path was the gap left behind when Flow B was paced: 104 new-
    listing candidates fanned out unpaced tripped the same rate limit
    Flow B used to, in a completely different function nobody had
    touched -- confirmed live, two Perform Sync runs in one hour dying
    at this exact step after backfill and reconciliation had already
    succeeded.
    """
    return _RequestPacer(
        competitor_pricing_service.OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS
        if min_request_interval is None else min_request_interval
    )


def request_from_identity(key, identity: dict) -> dict:
    """Like request_from_binding, but for a candidate with no RemoteProductBinding.

    Used for day-to-day new-listing candidates written via scryfall_id, which
    never need a Mana Pool product_id resolved up front.
    """
    allowed = eligible_competitor_conditions(identity["condition_id"])
    return {
        "key": key,
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
    manual_overrides=(), min_request_interval: float | None = None,
    skip_competitor_tier: bool = False, reviewed_price_by_binding_id: dict | None = None,
    bought_in_price_by_binding_id: dict | None = None, cost_markup_multiplier: float = 2.0,
) -> dict:
    """Verify competitor prices without seller inventory or any write calls.

    See price_new_listing_candidates for skip_competitor_tier -- same
    shape here, keyed by binding id (this function has no session access
    to look up a card's own price itself, so the caller supplies
    ``reviewed_price_by_binding_id`` and ``bought_in_price_by_binding_id``).
    """
    pacer = _pacer(min_request_interval)
    binding_by_id = {binding.id: binding for binding in bindings}
    reviewed_price_by_binding_id = reviewed_price_by_binding_id or {}
    bought_in_price_by_binding_id = bought_in_price_by_binding_id or {}
    requests = [request_from_binding(binding) for binding in bindings]
    requests.sort(key=lambda row: row["binding_id"])
    selected_ids = []
    holds = {}
    calls = retries = 0
    if skip_competitor_tier:
        for request in requests:
            holds[request["binding_id"]] = "Competitor pricing skipped for first-time listing"
    else:
        for start in range(0, len(requests), batch_size):
            remaining = requests[start:start + batch_size]
            while remaining:
                calls += 1
                pacer.wait()
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
        if reason in _NO_COMPETITOR_REASONS and market_catalog_call:
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
        if reason in _NO_COMPETITOR_REASONS:
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
        if skip_competitor_tier and reason in _NO_COMPETITOR_REASONS:
            reviewed_price = reviewed_price_by_binding_id.get(request["binding_id"])
            if reviewed_price:
                from pricing_decision_service import evidence_hash
                target_price = max(int(reviewed_price), floor_cents)
                reviewed_evidence = {
                    "source_classification": "reviewed_inventory_price",
                    "product_id": request["product_id"], "identity": request["identity"],
                    "reviewed_price_cents": int(reviewed_price), "pricing_floor_cents": floor_cents,
                }
                results.append({
                    "binding_id": request["binding_id"], "product_id": request["product_id"],
                    "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                    "status": "priced",
                    "reason": "Published at reviewed inventory price pending competitive re-pricing",
                    "target_price_cents": target_price,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "reviewed_inventory_price", "price_source": "reviewed_inventory",
                    "floor_applied": int(reviewed_price) < floor_cents,
                    "evidence_hash": evidence_hash(reviewed_evidence),
                })
                continue
            bought_in_price = bought_in_price_by_binding_id.get(request["binding_id"])
            if bought_in_price:
                from pricing_decision_service import evidence_hash
                marked_up = round(int(bought_in_price) * cost_markup_multiplier)
                target_price = max(marked_up, floor_cents)
                cost_evidence = {
                    "source_classification": "cost_plus_markup",
                    "product_id": request["product_id"], "identity": request["identity"],
                    "bought_in_price_cents": int(bought_in_price),
                    "cost_markup_multiplier": cost_markup_multiplier,
                    "pricing_floor_cents": floor_cents,
                }
                results.append({
                    "binding_id": request["binding_id"], "product_id": request["product_id"],
                    "identity": request["identity"], "allowed_conditions": request["allowed_conditions"],
                    "status": "priced",
                    "reason": "Published at cost-plus-markup pending competitive re-pricing",
                    "target_price_cents": target_price,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "cost_plus_markup", "price_source": "cost_plus_markup",
                    "floor_applied": marked_up < floor_cents,
                    "evidence_hash": evidence_hash(cost_evidence),
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


_NO_COMPETITOR_REASONS = {
    "No seller-excluded competitor satisfies this request",
    "Competitor pricing skipped for first-time listing",
}


def price_new_listing_candidates(
    candidates: list[dict],
    optimizer_call, listings_call, seller_id,
    batch_size=20, undercut_cents=5, floor_cents=65, market_catalog_call=None,
    manual_overrides=(), min_request_interval: float | None = None,
    skip_competitor_tier: bool = False, cost_markup_multiplier: float = 2.0,
) -> dict:
    """Competitor -> exact-printing market fallback -> reviewed manual
    override -> reviewed inventory price -> cost-plus-markup -> HOLD, for
    candidates that have never been listed on Mana Pool and have no
    RemoteProductBinding.

    The manual-override tier is anchored by identity_hash rather than a
    binding id (see manual_price_override_service.valid_override_for_identity)
    -- this path has nothing to bind to yet, unlike price_initial_bindings.
    A candidate that can't be priced automatically, and has no reviewed
    override or reviewed inventory price either, stays HELD.

    ``candidates`` is a list of ``{"key": ..., "identity": {...}}`` (see
    ``request_from_identity``), optionally carrying ``card_reviewed_price_cents``
    (the card's own current_price/price_usd -- named to avoid colliding
    with new_listing_upload_service.py's unrelated "reviewed_price_cents",
    the preview-time target price shown to the operator) for the
    reviewed-inventory-price tier, and ``card_bought_in_price_cents`` (what
    CardFoundry paid for the card) for the cost-plus-markup tier.
    ``market_catalog_call`` should resolve Mana Pool market evidence by
    scryfall_id (e.g. ``manapool_service.get_single_catalog_by_scryfall_ids``).

    ``skip_competitor_tier=True`` skips the optimizer entirely -- no calls
    at all -- for first-time publishing, where getting the card listed now
    matters more than a competitively-checked price on day one; Flow B's
    regular competitive re-pricing (competitor_pricing_service.py) picks up
    freshly-listed inventory on its own next run regardless. A candidate
    with no market or manual price either still publishes, at its own
    reviewed inventory price (clamped to the floor), or -- if that's also
    unknown -- at ``cost_markup_multiplier`` times its own bought_in_price
    (also clamped to the floor), rather than holding. Only a candidate with
    no reviewed price and no bought_in_price either still holds.
    """
    pacer = _pacer(min_request_interval)
    requests = [request_from_identity(row["key"], row["identity"]) for row in candidates]
    requests.sort(key=lambda row: row["key"])
    card_reviewed_price_by_key = {
        tuple(row["key"]): row.get("card_reviewed_price_cents") for row in candidates
    }
    card_bought_in_price_by_key = {
        tuple(row["key"]): row.get("card_bought_in_price_cents") for row in candidates
    }
    selected_ids = []
    holds = {}
    calls = retries = 0
    if skip_competitor_tier:
        for request in requests:
            holds[request["key"]] = "Competitor pricing skipped for first-time listing"
    else:
        for start in range(0, len(requests), batch_size):
            remaining = requests[start:start + batch_size]
            while remaining:
                calls += 1
                pacer.wait()
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
                        holds[request["key"]] = "Optimizer conflict could not be mapped"
                    break
                next_remaining = []
                for index, request in enumerate(remaining):
                    if index in indexes:
                        holds[request["key"]] = "No seller-excluded competitor satisfies this request"
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
        reason = holds.get(request["key"])
        matches = [] if reason else [
            listing for inventory_id, listing in listing_by_id.items()
            if inventory_id not in assigned and listing_matches_request(listing, request)
        ]
        if not reason and len(matches) != 1:
            reason = "No exact resolved competitor matched" if not matches else "Ambiguous competitor mapping"
        if reason in _NO_COMPETITOR_REASONS and market_catalog_call:
            scryfall_id = request["identity"].get("scryfall_id")
            payload = market_catalog_call([scryfall_id]) if scryfall_id else {"data": []}
            market = market_evidence_from_catalog(request["identity"], payload)
            decision = market_decision(market, floor_cents)
            if decision["status"] == "priced":
                results.append({
                    "key": request["key"], "identity": request["identity"],
                    "allowed_conditions": request["allowed_conditions"],
                    "status": "priced", "reason": "Trustworthy exact-printing market fallback",
                    "target_price_cents": decision["target_price_cents"],
                    "price_classification": decision["classification"], "price_source": "market",
                    "floor_applied": decision["floor_applied"], "market_evidence": market,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "evidence_hash": decision["evidence_hash"],
                })
                continue
        if reason in _NO_COMPETITOR_REASONS:
            override = valid_override_for_identity(request["identity"], manual_overrides, floor_cents)
            if override:
                manual_evidence = {
                    "source_classification": "manual_price_override",
                    "manual_override_evidence_hash": override.evidence_hash,
                    "identity_hash": override.identity_hash,
                    "identity": request["identity"],
                    "manual_price_cents": override.manual_price_cents,
                    "note": override.note, "pricing_floor_cents": floor_cents,
                    "automatic_competitor_status": "unavailable",
                    "automatic_market_status": "unavailable",
                }
                from pricing_decision_service import evidence_hash
                results.append({
                    "key": request["key"], "identity": request["identity"],
                    "allowed_conditions": request["allowed_conditions"],
                    "status": "priced", "reason": "Reviewed manual fallback after current automatic HOLD",
                    "target_price_cents": override.manual_price_cents,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "manual_price_override", "price_source": "manual",
                    "floor_applied": False, "manual_evidence": manual_evidence,
                    "evidence_hash": evidence_hash(manual_evidence),
                })
                continue
        if skip_competitor_tier and reason in _NO_COMPETITOR_REASONS:
            reviewed_price = card_reviewed_price_by_key.get(tuple(request["key"]))
            if reviewed_price:
                from pricing_decision_service import evidence_hash
                target_price = max(int(reviewed_price), floor_cents)
                reviewed_evidence = {
                    "source_classification": "reviewed_inventory_price",
                    "identity": request["identity"], "reviewed_price_cents": int(reviewed_price),
                    "pricing_floor_cents": floor_cents,
                }
                results.append({
                    "key": request["key"], "identity": request["identity"],
                    "allowed_conditions": request["allowed_conditions"],
                    "status": "priced",
                    "reason": "Published at reviewed inventory price pending competitive re-pricing",
                    "target_price_cents": target_price,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "reviewed_inventory_price", "price_source": "reviewed_inventory",
                    "floor_applied": int(reviewed_price) < floor_cents,
                    "evidence_hash": evidence_hash(reviewed_evidence),
                })
                continue
            bought_in_price = card_bought_in_price_by_key.get(tuple(request["key"]))
            if bought_in_price:
                from pricing_decision_service import evidence_hash
                marked_up = round(int(bought_in_price) * cost_markup_multiplier)
                target_price = max(marked_up, floor_cents)
                cost_evidence = {
                    "source_classification": "cost_plus_markup",
                    "identity": request["identity"], "bought_in_price_cents": int(bought_in_price),
                    "cost_markup_multiplier": cost_markup_multiplier,
                    "pricing_floor_cents": floor_cents,
                }
                results.append({
                    "key": request["key"], "identity": request["identity"],
                    "allowed_conditions": request["allowed_conditions"],
                    "status": "priced",
                    "reason": "Published at cost-plus-markup pending competitive re-pricing",
                    "target_price_cents": target_price,
                    "competitor_inventory_id": None, "competitor_price_cents": None,
                    "competitor_effective_as_of": None, "market_evidence": None,
                    "price_classification": "cost_plus_markup", "price_source": "cost_plus_markup",
                    "floor_applied": marked_up < floor_cents,
                    "evidence_hash": evidence_hash(cost_evidence),
                })
                continue
        if reason:
            results.append({
                "key": request["key"], "identity": request["identity"],
                "allowed_conditions": request["allowed_conditions"],
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
                "key": request["key"], "identity": request["identity"],
                "allowed_conditions": request["allowed_conditions"],
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
            "key": request["key"], "identity": request["identity"],
            "allowed_conditions": request["allowed_conditions"],
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
            "key": row["key"], "identity": row["identity"],
            "allowed_conditions": row["allowed_conditions"],
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
            "candidates": len(candidates), "priced": sum(r["status"] == "priced" for r in results),
            "held": sum(r["status"] == "hold" for r in results),
            "optimizer_calls": calls, "optimizer_retries": retries,
            "listing_ids_resolved": len(unique_ids),
        },
    }
