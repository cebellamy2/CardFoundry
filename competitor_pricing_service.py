"""Read-only batched competitor pricing preview orchestration."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from pricing_diagnostic_service import (
    CONDITION_ORDER,
    build_exact_request,
    exact_identity,
    single_details,
)


SELLER_EXCLUSION_ID = "69340688-c3a9-451d-93e6-031a0e3a73ad"
OPTIMIZER_BATCH_LIMIT = 2000
DEFAULT_OPTIMIZER_BATCH_SIZE = 20
OPTIMIZER_CONCURRENCY = 4
LISTING_LOOKUP_CHUNK = 100


def _member_from_item(item: dict) -> dict:
    single = single_details(item)
    return {
        "inventory_id": str(item.get("id") or ""),
        "product_id": str(item.get("product_id") or ""),
        "name": str(single.get("name") or "Unknown card"),
        "set_code": str(single.get("set") or ""),
        "collector_number": str(single.get("number") or ""),
        "language_id": str(single.get("language_id") or "").upper(),
        "condition_id": str(single.get("condition_id") or "").upper(),
        "finish_id": str(single.get("finish_id") or "").upper(),
        "quantity": int(item.get("quantity") or 0),
        "current_price": int(item.get("price_cents") or 0),
    }


def _hold(member: dict, reason: str, allowed=None) -> dict:
    return {
        **member,
        "competitor_inventory_id": None,
        "competitor_price": None,
        "competitor_condition": None,
        "competitor_effective_as_of": None,
        "allowed_conditions": list(allowed or []),
        "target_price": member.get("current_price"),
        "change_cents": 0,
        "action": "hold",
        "validation_status": "hold",
        "validation_reason": reason,
        "preview_only": True,
    }


def deduplicate_competitor_requests(
    seller_inventory: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Collapse rows with the same exact printing/variant request."""
    grouped = {}
    holds = []

    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        member = _member_from_item(item)
        if member["quantity"] < 1:
            holds.append(_hold(member, "No live quantity"))
            continue
        if member["current_price"] < 1:
            holds.append(_hold(member, "No valid current price"))
            continue

        try:
            request = build_exact_request(item)
        except ValueError as exc:
            holds.append(_hold(member, str(exc)))
            continue

        key = (request["identity"], request["condition_id"])
        if key not in grouped:
            grouped[key] = {**request, "members": []}
        grouped[key]["members"].append(member)

    requests = list(grouped.values())
    requests.sort(key=lambda row: (row["identity"], row["condition_id"]))
    return requests, holds


def partition_optimizer_requests(
    requests: list[dict],
    batch_limit: int = DEFAULT_OPTIMIZER_BATCH_SIZE,
) -> list[list[dict]]:
    """Ensure one condition ladder per printing/language/finish per batch."""
    if batch_limit < 1 or batch_limit > OPTIMIZER_BATCH_LIMIT:
        raise ValueError("batch_limit must be between 1 and 2000.")

    by_identity = defaultdict(list)
    for request in requests:
        by_identity[request["identity"]].append(request)

    lanes = []
    rank = {condition: index for index, condition in enumerate(CONDITION_ORDER)}
    for identity in sorted(by_identity):
        variants = sorted(
            by_identity[identity],
            key=lambda row: rank.get(row["condition_id"], len(rank)),
        )
        while len(lanes) < len(variants):
            lanes.append([])
        for index, request in enumerate(variants):
            lanes[index].append(request)

    return [
        lane[start:start + batch_limit]
        for lane in lanes
        for start in range(0, len(lane), batch_limit)
    ]


def _conflict_indexes(response: dict, size: int) -> set[int]:
    indexes = set()
    for detail in response.get("_conflicts") or []:
        item = detail.get("item") or {}
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < size:
            indexes.add(index)
    return indexes


def _validate_batch(
    requests: list[dict],
    optimized: dict,
    listing_by_id: dict[str, dict],
    undercut_cents: int,
    floor_cents: int,
) -> list[dict]:
    selected = optimized.get("cart") or []
    selected_by_id = {}
    invalid_selection = False
    for row in selected:
        inventory_id = str(row.get("inventory_id") or "")
        quantity = int(row.get("quantity_selected") or 0)
        if not inventory_id or quantity != 1 or inventory_id in selected_by_id:
            invalid_selection = True
            continue
        selected_by_id[inventory_id] = row

    validated = []
    assigned = set()
    for request in requests:
        matches = []
        for inventory_id in selected_by_id:
            listing = listing_by_id.get(inventory_id)
            if listing and exact_identity(listing) == request["identity"]:
                matches.append(listing)

        reason = None
        listing = None
        if invalid_selection:
            reason = "Optimizer returned an invalid or duplicate selection"
        elif len(matches) == 0:
            reason = "No exact resolved competitor listing matched"
        elif len(matches) > 1:
            reason = "Multiple resolved competitor listings matched"
        else:
            listing = matches[0]
            assigned.add(str(listing.get("id") or ""))
            single = single_details(listing)
            condition = str(single.get("condition_id") or "").upper()
            if condition not in request["allowed_conditions"]:
                reason = "Resolved competitor has a worse or unknown condition"
            elif int(listing.get("quantity") or 0) < 1:
                reason = "Resolved competitor listing is stale or unavailable"
            elif not listing.get("effective_as_of"):
                reason = "Resolved competitor listing has no freshness timestamp"
            elif not isinstance(listing.get("price_cents"), (int, float)):
                reason = "Resolved competitor listing has no valid price"
            elif int(listing["price_cents"]) < 1:
                reason = "Resolved competitor listing has no valid price"

        for member in request["members"]:
            if reason or not listing:
                validated.append(_hold(member, reason or "Invalid mapping", request["allowed_conditions"]))
                continue

            single = single_details(listing)
            competitor_price = int(listing["price_cents"])
            target = max(competitor_price - undercut_cents, floor_cents)
            current = int(member["current_price"])
            if target > current:
                action = "increase"
            elif target < current:
                action = "decrease"
            else:
                action = "hold"
            validated.append({
                **member,
                "competitor_inventory_id": str(listing.get("id") or ""),
                "competitor_product_id": str(listing.get("product_id") or ""),
                "competitor_price": competitor_price,
                "competitor_condition": str(single.get("condition_id") or "").upper(),
                "competitor_effective_as_of": listing.get("effective_as_of"),
                "allowed_conditions": list(request["allowed_conditions"]),
                "target_price": target,
                "change_cents": target - current,
                "action": action,
                "validation_status": "passed",
                "validation_reason": "Exact seller-excluded competitor listing validated",
                "floor_applied": target == floor_cents and competitor_price - undercut_cents < floor_cents,
                "preview_only": True,
            })

    unassigned = set(selected_by_id) - assigned
    if unassigned:
        for row in validated:
            if row["validation_status"] == "passed":
                row.update(_hold(
                    row,
                    "Optimizer returned an unassignable competitor listing",
                    row["allowed_conditions"],
                ))
    return validated


def _process_optimizer_batch(
    original_batch: list[dict],
    optimizer_call,
    seller_id: str,
) -> dict:
    """Process one independent batch; keep its dependent retries serial."""
    queue = [list(original_batch)]
    successful = []
    holds = []
    calls = 0
    failures = 0
    retries = 0

    while queue:
        remaining = queue.pop(0)
        if calls:
            retries += 1
        calls += 1
        try:
            response = optimizer_call(
                [request["cart_item"] for request in remaining],
                seller_id,
            )
        except Exception as exc:
            failures += 1
            if len(remaining) > 1:
                midpoint = len(remaining) // 2
                queue.insert(0, remaining[midpoint:])
                queue.insert(0, remaining[:midpoint])
            else:
                request = remaining[0]
                for member in request["members"]:
                    holds.append(_hold(
                        member,
                        f"Optimizer request failed: {type(exc).__name__}",
                        request["allowed_conditions"],
                    ))
            continue

        conflicts = response.get("_conflicts") or []
        if conflicts:
            indexes = _conflict_indexes(response, len(remaining))
            if not indexes:
                for request in remaining:
                    for member in request["members"]:
                        holds.append(_hold(
                            member,
                            "Optimizer conflict could not be mapped to a request",
                            request["allowed_conditions"],
                        ))
                continue

            next_remaining = []
            for index, request in enumerate(remaining):
                if index in indexes:
                    for member in request["members"]:
                        holds.append(_hold(
                            member,
                            "No seller-excluded competitor satisfies this request",
                            request["allowed_conditions"],
                        ))
                else:
                    next_remaining.append(request)
            if next_remaining:
                queue.insert(0, next_remaining)
            continue

        successful.append((remaining, response))

    return {
        "successful": successful,
        "holds": holds,
        "calls": calls,
        "failures": failures,
        "retries": retries,
    }


def build_batched_competitor_preview(
    seller_inventory: list[dict],
    optimizer_call,
    listings_call,
    seller_id: str = SELLER_EXCLUSION_ID,
    undercut_cents: int = 5,
    floor_cents: int = 65,
    batch_limit: int = DEFAULT_OPTIMIZER_BATCH_SIZE,
    listing_chunk: int = LISTING_LOOKUP_CHUNK,
    progress_callback=None,
) -> dict:
    """Build a fail-closed, fully seller-excluded, read-only preview."""
    requests, audit_rows = deduplicate_competitor_requests(seller_inventory)
    batches = partition_optimizer_requests(requests, batch_limit=batch_limit)
    progress = {
        "stage": "optimizer",
        "inventory_rows": len(seller_inventory),
        "deduplicated_requests": len(requests),
        "optimizer_batches_total": len(batches),
        "optimizer_batches_completed": 0,
        "optimizer_calls": 0,
        "optimizer_failures": 0,
        "optimizer_retries": 0,
        "listing_chunks_total": 0,
        "listing_chunks_completed": 0,
    }
    if progress_callback:
        progress_callback(dict(progress))

    batch_results = {}
    with ThreadPoolExecutor(max_workers=OPTIMIZER_CONCURRENCY) as executor:
        futures = {
            executor.submit(
                _process_optimizer_batch,
                original_batch,
                optimizer_call,
                seller_id,
            ): index
            for index, original_batch in enumerate(batches)
        }
        for future in as_completed(futures):
            batch_results[futures[future]] = future.result()
            result = batch_results[futures[future]]
            progress["optimizer_calls"] += result["calls"]
            progress["optimizer_failures"] += result["failures"]
            progress["optimizer_retries"] += result["retries"]
            progress["optimizer_batches_completed"] += 1
            if progress_callback:
                progress_callback(dict(progress))

    successful = []
    for index in range(len(batches)):
        result = batch_results[index]
        successful.extend(result["successful"])
        audit_rows.extend(result["holds"])

    selected_ids = []
    for _, response in successful:
        selected_ids.extend(
            str(row.get("inventory_id") or "")
            for row in response.get("cart") or []
            if row.get("inventory_id")
        )
    unique_ids = list(dict.fromkeys(selected_ids))
    chunks = [
        unique_ids[start:start + listing_chunk]
        for start in range(0, len(unique_ids), listing_chunk)
    ]
    progress["stage"] = "listing_resolution"
    progress["listing_chunks_total"] = len(chunks)
    if progress_callback:
        progress_callback(dict(progress))

    listing_by_id = {}
    for chunk in chunks:
        for listing in listings_call(chunk):
            inventory_id = str(listing.get("id") or "")
            if inventory_id in listing_by_id:
                listing_by_id[inventory_id] = None
            elif inventory_id:
                listing_by_id[inventory_id] = listing
        progress["listing_chunks_completed"] += 1
        if progress_callback:
            progress_callback(dict(progress))

    for batch, response in successful:
        audit_rows.extend(_validate_batch(
            batch,
            response,
            listing_by_id,
            int(undercut_cents),
            int(floor_cents),
        ))

    changes = [row for row in audit_rows if row["action"] in {"increase", "decrease"}]
    holds = [row for row in audit_rows if row["action"] == "hold"]
    changes.sort(key=lambda row: (row["action"] != "increase", -abs(row["change_cents"])))
    progress["stage"] = "complete"
    if progress_callback:
        progress_callback(dict(progress))

    return {
        "changes": changes,
        "holds": holds,
        "audit_rows": audit_rows,
        "progress": progress,
        "summary": {
            "seller_items": len(seller_inventory),
            "deduplicated_requests": len(requests),
            "optimizer_batches": len(batches),
            "optimizer_calls": progress["optimizer_calls"],
            "optimizer_failures": progress["optimizer_failures"],
            "optimizer_retries": progress["optimizer_retries"],
            "listing_calls": len(chunks),
            "changes": len(changes),
            "increases": sum(row["action"] == "increase" for row in changes),
            "decreases": sum(row["action"] == "decrease" for row in changes),
            "holds": len(holds),
            "skipped": 0,
            "floor_applied_count": sum(bool(row.get("floor_applied")) for row in changes),
            "total_change_cents": sum(row["change_cents"] for row in changes),
            "verified_increases": sum(row["action"] == "increase" for row in changes),
        },
    }
