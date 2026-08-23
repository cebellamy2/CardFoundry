"""Batched competitor pricing preview orchestration, plus a guarded apply."""

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from pricing_diagnostic_service import (
    CONDITION_ORDER,
    build_pricing_request,
    pricing_identity,
    single_details,
)
from pricing_decision_service import (
    competitor_decision, existing_floor_decision, market_decision,
    market_evidence_from_catalog,
)


SELLER_EXCLUSION_ID = "69340688-c3a9-451d-93e6-031a0e3a73ad"
OPTIMIZER_BATCH_LIMIT = 2000
DEFAULT_OPTIMIZER_BATCH_SIZE = 20
OPTIMIZER_CONCURRENCY = 4
# A floor on the gap between optimizer requests, applied across all
# workers, so the real request rate stops depending on how fast Mana Pool
# happens to answer. The live incident started here: ~264 batches fanned
# out at 4-way concurrency with no pacing at all tripped the rate limit
# before any retry logic was involved. This bounds the burst; the retry
# budget in manapool_service handles what still gets through. Raise it for
# a gentler run, set it to 0 to restore the old unpaced behavior.
OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS = float(
    os.environ.get("OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS", "1.0")
)
LISTING_LOOKUP_CHUNK = 100
DEFAULT_PRICE_DRIFT_TOLERANCE = 0.10


class CompetitorPricingError(ValueError):
    pass


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
        "competitor_language": None,
        "competitor_finish": None,
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
            request = build_pricing_request(item)
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
            if listing and pricing_identity(listing) == request["identity"]:
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
            decision = competitor_decision({
                "inventory_id": str(listing.get("id") or ""),
                "product_id": str(listing.get("product_id") or ""),
                "seller_id": listing.get("seller_id"),
                "language_id": str(single.get("language_id") or "").upper(),
                "condition_id": str(single.get("condition_id") or "").upper(),
                "finish_id": str(single.get("finish_id") or "").upper(),
                "price_cents": competitor_price,
                "effective_as_of": listing.get("effective_as_of"),
            }, undercut_cents, floor_cents)
            target = decision["target_price_cents"]
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
                "competitor_language": str(single.get("language_id") or "").upper(),
                "competitor_finish": str(single.get("finish_id") or "").upper(),
                "competitor_effective_as_of": listing.get("effective_as_of"),
                "allowed_conditions": list(request["allowed_conditions"]),
                "target_price": target,
                "change_cents": target - current,
                "action": action,
                "validation_status": "passed",
                "validation_reason": "Exact seller-excluded competitor listing validated",
                "floor_applied": decision["floor_applied"],
                "price_classification": decision["classification"],
                "price_source": decision["price_source"],
                "pricing_evidence_hash": decision["evidence_hash"],
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


class _RequestPacer:
    """Spaces out the *starts* of optimizer requests across worker threads.

    A worker reserves the next slot while holding the lock and then sleeps
    outside it, so N workers stagger onto N successive slots instead of all
    queueing behind one sleeping thread. An interval of 0 disables pacing
    and costs nothing.
    """

    def __init__(self, min_interval: float, sleep=None, now=None):
        self._min_interval = max(0.0, float(min_interval))
        # Resolved here rather than as default arguments, which would bind
        # time.sleep once at import and ignore anything patched later.
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._next_slot = None

    def wait(self) -> float:
        """Block until this caller's slot, returning how long that took."""
        if not self._min_interval:
            return 0.0
        with self._lock:
            now = self._now()
            slot = now if self._next_slot is None else max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        delay = slot - now
        if delay > 0:
            self._sleep(delay)
        return delay


def _is_rate_limit_failure(exc: Exception) -> bool:
    """A 429 means "you are sending too much", not "this batch is too
    big". Bisecting one doubles the request count against the limiter
    that just turned us away, so these are held rather than split.
    Duck-typed on the response instead of importing httpx, since the
    optimizer call is injected and this module stays transport-agnostic.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


def _process_optimizer_batch(
    original_batch: list[dict],
    optimizer_call,
    seller_id: str,
    pacer: "_RequestPacer | None" = None,
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
            if pacer is not None:
                pacer.wait()
            response = optimizer_call(
                [request["cart_item"] for request in remaining],
                seller_id,
            )
        except Exception as exc:
            failures += 1
            if _is_rate_limit_failure(exc):
                for request in remaining:
                    for member in request["members"]:
                        holds.append(_hold(
                            member,
                            "Mana Pool rate limit still closed; not priced this run",
                            request["allowed_conditions"],
                        ))
                continue
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
    market_catalog_call=None,
    min_request_interval: float | None = None,
) -> dict:
    """Build a fail-closed, fully seller-excluded, read-only preview."""
    # Read at call time, not as a default argument, so the module constant
    # stays overridable (tests set it to 0; a manual run can raise it).
    pacer = _RequestPacer(
        OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS
        if min_request_interval is None
        else min_request_interval
    )
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
                pacer,
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

    if market_catalog_call:
        fallback_rows = [row for row in audit_rows if (
            row.get("validation_status") == "hold"
            and row.get("validation_reason") == "No seller-excluded competitor satisfies this request"
            and row.get("product_id")
        )]
        catalog_by_product = {}
        product_ids = list(dict.fromkeys(row["product_id"] for row in fallback_rows))
        for start in range(0, len(product_ids), 100):
            payload = market_catalog_call(product_ids[start:start + 100])
            meta = payload.get("meta") or {}
            for product in payload.get("data") or []:
                for variant in product.get("variants") or []:
                    product_id = str(variant.get("product_id") or "")
                    if product_id:
                        catalog_by_product[product_id] = {"meta": meta, "data": [product]}
        for row in fallback_rows:
            identity = {
                "name": row["name"], "set_code": row["set_code"],
                "collector_number": row["collector_number"],
                "scryfall_id": None, "finish_id": row["finish_id"],
            }
            payload = catalog_by_product.get(row["product_id"], {"meta": {}, "data": []})
            data = payload.get("data") or []
            if len(data) == 1:
                identity["scryfall_id"] = data[0].get("scryfall_id")
            market = market_evidence_from_catalog(identity, payload)
            decision = market_decision(market, floor_cents)
            if decision["status"] != "priced":
                row["price_classification"] = decision["classification"]
                row["pricing_evidence_hash"] = decision["evidence_hash"]
                continue
            current = int(row["current_price"])
            target = decision["target_price_cents"]
            row.update({
                "target_price": target, "change_cents": target - current,
                "action": "increase" if target > current else "decrease" if target < current else "hold",
                "validation_status": "passed", "validation_reason": "Trustworthy exact-printing market fallback",
                "market_price_cents": market["price_cents"], "market_evidence": market,
                "price_classification": decision["classification"], "price_source": "market",
                "floor_applied": decision["floor_applied"], "pricing_evidence_hash": decision["evidence_hash"],
            })

    # The configured floor is owner policy, not inferred market evidence. It is
    # therefore sufficient to repair an already-below-floor listing even when
    # both automatic price sources are unavailable.
    for row in audit_rows:
        current = int(row.get("current_price") or 0)
        target = row.get("target_price")
        if current < int(floor_cents) and (
            row.get("validation_status") != "passed"
            or target is None or int(target) < int(floor_cents)
        ):
            decision = existing_floor_decision(current, floor_cents)
            row.update({
                "target_price": decision["target_price_cents"],
                "change_cents": decision["target_price_cents"] - current,
                "action": "increase", "validation_status": "passed",
                "validation_reason": "Owner-configured absolute pricing floor",
                "floor_applied": True,
                "price_classification": decision["classification"],
                "price_source": decision["price_source"],
                "pricing_evidence_hash": decision["evidence_hash"],
            })

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


def _apply_fresh_target(row, fresh_target, price_drift_tolerance, updates, excluded, repriced):
    """Shared drift-check/write-append tail for both competitor and market
    rows: a small move applies at the fresh price ("repriced"); a move at
    or past tolerance excludes the row instead of writing a stale number.
    """
    reviewed_target = int(row["target_price"])
    drift_ratio = (
        1.0 if reviewed_target == 0 and fresh_target
        else 0.0 if reviewed_target == 0
        else abs(fresh_target - reviewed_target) / reviewed_target
    )
    if drift_ratio >= price_drift_tolerance:
        excluded.append({
            **row,
            "exclusion_reason": "Price basis moved beyond tolerance since preview",
            "fresh_target_price": fresh_target,
        })
        return

    if fresh_target != reviewed_target:
        repriced.append({
            **row,
            "reviewed_target_price": reviewed_target,
            "fresh_target_price": fresh_target,
        })

    updates.append({
        "product_type": "mtg_single",
        "product_id": row["product_id"],
        "price_cents": fresh_target,
        "quantity": None,
    })


def apply_full_competitor_preview(
    preview: dict,
    sellable_products: set,
    fresh_listing_loader,
    product_writer,
    undercut_cents: int,
    floor_cents: int,
    price_drift_tolerance: float = DEFAULT_PRICE_DRIFT_TOLERANCE,
    listing_chunk: int = LISTING_LOOKUP_CHUNK,
    market_catalog_loader=None,
) -> dict:
    """Re-verify each row's competitor or market basis immediately before writing.

    Mirrors new_listing_upload_service.apply_new_listing_preview's shape:
    a row whose basis moved by less than ``price_drift_tolerance`` is
    still applied, but at the freshly recomputed price ("repriced"), not
    the stale reviewed one; only a move at or past the tolerance excludes
    the row entirely. Every excluded/repriced row is reported with why,
    batch-isolated -- one row's staleness never blocks the rest.

    Most rows resolved to a specific competitor listing at preview time;
    those are re-checked by re-fetching that exact listing (via
    ``fresh_listing_loader``, the same listings-by-id call the preview
    used) rather than re-running the /buyer/optimizer search -- doing
    that here would reintroduce the optimizer's own confirmed
    nondeterminism into the very check meant to guard against staleness,
    and could select a different competitor than the one already
    reviewed. A minority of rows have no competitor at all and were
    priced from Mana Pool's own catalog market price instead
    (price_source == "market"); those are re-verified against a fresh
    catalog lookup via ``market_catalog_loader`` instead of a listing --
    if that loader isn't supplied, market rows are excluded rather than
    applied unverified.
    """
    changed_rows = [
        row for row in preview.get("changes") or []
        if row.get("action") in ("increase", "decrease")
    ]
    if not changed_rows:
        raise CompetitorPricingError("This preview has no price changes to apply.")

    competitor_rows = [row for row in changed_rows if row.get("price_source") != "market"]
    market_rows = [row for row in changed_rows if row.get("price_source") == "market"]

    competitor_ids = sorted({
        str(row["competitor_inventory_id"]) for row in competitor_rows
        if row.get("competitor_inventory_id")
    })
    fresh_by_id = {}
    for start in range(0, len(competitor_ids), listing_chunk):
        chunk = competitor_ids[start:start + listing_chunk]
        for listing in fresh_listing_loader(chunk):
            inventory_id = str(listing.get("id") or "")
            if inventory_id:
                fresh_by_id[inventory_id] = listing

    updates = []
    excluded = []
    repriced = []

    for row in competitor_rows:
        product_id = row.get("product_id")
        if product_id not in sellable_products:
            excluded.append({**row, "exclusion_reason": "No longer locally sellable"})
            continue

        competitor_id = str(row.get("competitor_inventory_id") or "")
        fresh_listing = fresh_by_id.get(competitor_id)
        if not fresh_listing:
            excluded.append({**row, "exclusion_reason": "Competitor listing no longer exists"})
            continue

        single = single_details(fresh_listing)
        condition = str(single.get("condition_id") or "").upper()
        allowed = row.get("allowed_conditions") or []
        if condition not in allowed:
            excluded.append({
                **row,
                "exclusion_reason": "Competitor listing's condition is no longer valid for this request",
            })
            continue
        if int(fresh_listing.get("quantity") or 0) < 1:
            excluded.append({**row, "exclusion_reason": "Competitor listing is stale or unavailable"})
            continue
        if not fresh_listing.get("effective_as_of"):
            excluded.append({**row, "exclusion_reason": "Competitor listing has no freshness timestamp"})
            continue
        if not isinstance(fresh_listing.get("price_cents"), (int, float)) or int(fresh_listing["price_cents"]) < 1:
            excluded.append({**row, "exclusion_reason": "Competitor listing has no valid price"})
            continue

        fresh_price = int(fresh_listing["price_cents"])
        decision = competitor_decision({
            "inventory_id": competitor_id,
            "product_id": str(fresh_listing.get("product_id") or ""),
            "language_id": str(single.get("language_id") or "").upper(),
            "condition_id": condition,
            "finish_id": str(single.get("finish_id") or "").upper(),
            "price_cents": fresh_price,
            "effective_as_of": fresh_listing.get("effective_as_of"),
        }, undercut_cents, floor_cents)
        _apply_fresh_target(
            row, decision["target_price_cents"], price_drift_tolerance,
            updates, excluded, repriced,
        )

    if market_rows and not market_catalog_loader:
        for row in market_rows:
            excluded.append({
                **row,
                "exclusion_reason": "Market-fallback pricing cannot be re-verified for this apply",
            })
    elif market_rows:
        product_ids = list(dict.fromkeys(row["product_id"] for row in market_rows))
        catalog_by_product = {}
        for start in range(0, len(product_ids), 100):
            payload = market_catalog_loader(product_ids[start:start + 100])
            meta = payload.get("meta") or {}
            for product in payload.get("data") or []:
                for variant in product.get("variants") or []:
                    variant_product_id = str(variant.get("product_id") or "")
                    if variant_product_id:
                        catalog_by_product[variant_product_id] = {"meta": meta, "data": [product]}

        for row in market_rows:
            product_id = row.get("product_id")
            if product_id not in sellable_products:
                excluded.append({**row, "exclusion_reason": "No longer locally sellable"})
                continue

            identity = {
                "name": row.get("name"), "set_code": row.get("set_code"),
                "collector_number": row.get("collector_number"),
                "scryfall_id": (row.get("market_evidence") or {}).get("scryfall_id"),
                "finish_id": row.get("finish_id"),
            }
            payload = catalog_by_product.get(product_id, {"meta": {}, "data": []})
            market = market_evidence_from_catalog(identity, payload)
            decision = market_decision(market, floor_cents)
            if decision["status"] != "priced":
                excluded.append({
                    **row,
                    "exclusion_reason": "No trustworthy market evidence remains for this printing",
                })
                continue
            _apply_fresh_target(
                row, decision["target_price_cents"], price_drift_tolerance,
                updates, excluded, repriced,
            )

    if not updates:
        raise CompetitorPricingError(
            "None of the reviewed rows are still valid to apply -- local sellability "
            "or the competitor/market pricing basis changed since preview. "
            "Run a fresh preview."
        )

    responses = product_writer(updates)

    return {
        "updates": updates,
        "responses": responses,
        "excluded": excluded,
        "repriced": repriced,
    }
