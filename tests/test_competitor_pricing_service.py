import threading
import time

from competitor_pricing_service import (
    DEFAULT_OPTIMIZER_BATCH_SIZE,
    OPTIMIZER_CONCURRENCY,
    build_batched_competitor_preview,
    deduplicate_competitor_requests,
    partition_optimizer_requests,
)


def test_no_competitor_or_market_still_repairs_below_floor_current_price():
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", price=15)
    preview = build_batched_competitor_preview(
        [ours], lambda payload, seller: {"_conflicts": [{"item": {"index": 0}}]},
        lambda ids: [], floor_cents=65,
    )
    assert preview["changes"][0]["target_price"] == 65
    assert preview["changes"][0]["price_classification"] == "floor_corrected_existing"


def item(
    inventory_id,
    product_id,
    name,
    set_code,
    number,
    condition,
    finish="NF",
    price=100,
    language="EN",
    mtgjson_id=None,
):
    return {
        "id": inventory_id,
        "product_type": "mtg_single",
        "product_id": product_id,
        "price_cents": price,
        "quantity": 1,
        "effective_as_of": "2026-08-12T12:00:00Z",
        "product": {"single": {
            "name": name,
            "set": set_code,
            "number": number,
            "language_id": language,
            "condition_id": condition,
            "finish_id": finish,
            "mtgjson_id": mtgjson_id or f"mtg-{set_code}-{number}",
        }},
    }


def test_deduplicates_only_equivalent_requests():
    inventory = [
        item("a", "p-lp", "Card", "SET", "1", "LP"),
        item("b", "p-lp", "Card", "SET", "1", "LP", price=120),
        item("c", "p-nm", "Card", "SET", "1", "NM"),
        item("d", "other", "Card", "SET", "1", "LP", language="JA"),
    ]

    requests, holds = deduplicate_competitor_requests(inventory)

    assert holds == []
    # Pricing requests differing only by language are intentionally equivalent.
    assert len(requests) == 2
    assert sorted(len(request["members"]) for request in requests) == [1, 3]


def test_overlapping_condition_ladders_use_separate_batches_and_limit():
    inventory = [
        item("lp", "p-lp", "Card", "SET", "1", "LP"),
        item("mp", "p-mp", "Card", "SET", "1", "MP"),
        item("other", "other", "Other", "TWO", "2", "NM"),
    ]
    requests, _ = deduplicate_competitor_requests(inventory)

    batches = partition_optimizer_requests(requests, batch_limit=2)

    assert all(len(batch) <= 2 for batch in batches)
    for batch in batches:
        identities = [request["identity"] for request in batch]
        assert len(identities) == len(set(identities))
    assert len(batches) == 2


def test_production_default_uses_conservative_optimizer_batches():
    assert DEFAULT_OPTIMIZER_BATCH_SIZE == 20
    assert OPTIMIZER_CONCURRENCY == 4


def test_all_languages_requested_but_actual_winner_language_is_audited():
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", language="JA")
    competitor = item("comp", "comp-product", "Card", "SET", "1", "LP", price=475, language="DE")
    seen = {}
    def optimize(cart, seller):
        seen["languages"] = cart[0]["language_ids"]
        return {"cart":[{"inventory_id":"comp","quantity_selected":1}]}
    result = build_batched_competitor_preview([ours], optimize, lambda ids:[competitor])
    row = result["changes"][0]
    assert {"EN", "JA", "DE", "FR"}.issubset(seen["languages"])
    assert row["competitor_language"] == "DE"
    assert row["target_price"] == 470


def test_no_competitor_uses_exact_market_without_undercut():
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", price=100)
    def optimize(cart, seller): return {"cart":[],"_conflicts":[{"item":{"index":0}}]}
    def catalog(ids):
        return {"meta":{"as_of":"now"},"data":[{
            "name":"Card","set_code":"SET","number":"1","scryfall_id":"",
            "price_market":200,"variants":[{"product_id":"ours-product"}],
        }]}
    result = build_batched_competitor_preview(
        [ours], optimize, lambda ids:[], market_catalog_call=catalog,
    )
    row = result["changes"][0]
    assert row["target_price"] == 200
    assert row["price_classification"] == "market_price_fallback"


def test_full_preview_never_exceeds_four_concurrent_optimizer_calls():
    inventory = [
        item(f"ours-{index}", f"p-{index}", f"Card {index}", "SET", str(index), "NM")
        for index in range(8)
    ]
    competitors = {
        f"comp-{index}": item(
            f"comp-{index}", f"p-{index}", f"Card {index}", "SET", str(index),
            "NM", price=200,
        )
        for index in range(8)
    }
    lock = threading.Lock()
    active = 0
    peak = 0

    def optimize(cart, seller):
        nonlocal active, peak
        index = cart[0]["name"].removeprefix("Card ")
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"cart": [{
            "inventory_id": f"comp-{index}",
            "quantity_selected": 1,
        }]}

    preview = build_batched_competitor_preview(
        inventory,
        optimize,
        lambda ids: [competitors[value] for value in ids],
        batch_limit=1,
    )

    assert peak == 4
    assert preview["summary"]["optimizer_batches"] == 8
    assert preview["summary"]["optimizer_calls"] == 8


def test_optimizer_failure_does_not_corrupt_unrelated_batch_results():
    inventory = [
        item("ours-a", "a", "Alpha", "ONE", "1", "NM"),
        item("ours-b", "b", "Beta", "TWO", "2", "NM"),
        item("ours-c", "c", "Gamma", "THREE", "3", "NM"),
    ]
    competitors = {
        "comp-a": item("comp-a", "a", "Alpha", "ONE", "1", "NM", price=200),
        "comp-c": item("comp-c", "c", "Gamma", "THREE", "3", "NM", price=200),
    }

    def optimize(cart, seller):
        name = cart[0]["name"]
        if name == "Beta":
            raise TimeoutError("simulated")
        inventory_id = "comp-a" if name == "Alpha" else "comp-c"
        return {"cart": [{"inventory_id": inventory_id, "quantity_selected": 1}]}

    preview = build_batched_competitor_preview(
        inventory,
        optimize,
        lambda ids: [competitors[value] for value in ids],
        batch_limit=1,
    )

    by_name = {row["name"]: row for row in preview["audit_rows"]}
    assert by_name["Alpha"]["target_price"] == 195
    assert by_name["Gamma"]["target_price"] == 195
    assert by_name["Beta"]["action"] == "hold"
    assert by_name["Beta"]["validation_reason"] == "Optimizer request failed: TimeoutError"


def test_full_preview_maps_reversed_results_and_both_directions():
    inventory = [
        item("ours-a", "a-lp", "Alpha", "ONE", "1", "LP", price=100),
        item("ours-b", "b-nm", "Beta", "TWO", "2", "NM", price=200),
    ]
    competitors = {
        "comp-a": item("comp-a", "a-nm", "Alpha", "ONE", "1", "NM", price=150),
        "comp-b": item("comp-b", "b-nm", "Beta", "TWO", "2", "NM", price=100),
    }
    calls = []

    def optimize(cart, seller_id):
        calls.append((cart, seller_id))
        return {"cart": [
            {"inventory_id": "comp-b", "quantity_selected": 1},
            {"inventory_id": "comp-a", "quantity_selected": 1},
        ]}

    preview = build_batched_competitor_preview(
        inventory,
        optimize,
        lambda ids: [competitors[value] for value in reversed(ids)],
    )

    assert len(calls) == 1
    assert preview["summary"]["increases"] == 1
    assert preview["summary"]["decreases"] == 1
    by_name = {row["name"]: row for row in preview["changes"]}
    assert by_name["Alpha"]["target_price"] == 145
    assert by_name["Alpha"]["allowed_conditions"] == ["NM", "LP"]
    assert by_name["Beta"]["target_price"] == 95
    assert all(row["preview_only"] for row in preview["changes"])


def test_ambiguous_mapping_and_worse_condition_hold():
    inventory = [
        item("ours-a", "a-lp", "Alpha", "ONE", "1", "LP"),
        item("ours-b", "b-lp", "Beta", "TWO", "2", "LP"),
    ]
    listings = [
        item("a1", "a-nm", "Alpha", "ONE", "1", "NM", price=200),
        item("a2", "a-lp", "Alpha", "ONE", "1", "LP", price=210),
        item("b", "b-mp", "Beta", "TWO", "2", "MP", price=200),
    ]

    preview = build_batched_competitor_preview(
        inventory,
        lambda cart, seller: {"cart": [
            {"inventory_id": "a1", "quantity_selected": 1},
            {"inventory_id": "a2", "quantity_selected": 1},
            {"inventory_id": "b", "quantity_selected": 1},
        ]},
        lambda ids: listings,
    )

    assert preview["summary"]["changes"] == 0
    reasons = {row["name"]: row["validation_reason"] for row in preview["holds"]}
    assert "Multiple" in reasons["Alpha"]
    assert "worse" in reasons["Beta"]


def test_conflict_is_held_and_remaining_request_retried():
    inventory = [
        item("ours-a", "a", "Alpha", "ONE", "1", "NM"),
        item("ours-b", "b", "Beta", "TWO", "2", "NM"),
    ]
    competitor = item("comp-b", "b", "Beta", "TWO", "2", "NM", price=200)
    responses = [
        {"cart": [], "_conflicts": [{"item": {"index": 0}}]},
        {"cart": [{"inventory_id": "comp-b", "quantity_selected": 1}]},
    ]

    preview = build_batched_competitor_preview(
        inventory,
        lambda cart, seller: responses.pop(0),
        lambda ids: [competitor],
    )

    assert preview["summary"]["optimizer_calls"] == 2
    assert preview["summary"]["optimizer_retries"] == 1
    by_name = {row["name"]: row for row in preview["audit_rows"]}
    assert by_name["Alpha"]["action"] == "hold"
    assert by_name["Beta"]["action"] == "increase"


def test_floor_and_urza_regression():
    urza = item(
        "ours-urza", "urza-lp", "Urza's Ruinous Blast", "PDOM", "39s",
        "LP", finish="FO", price=498, mtgjson_id="urza-printing",
    )
    cheap = item("ours-cheap", "cheap", "Cheap", "SET", "2", "NM", price=80)
    listings = {
        "urza": item(
            "urza", "urza-nm", "Urza's Ruinous Blast", "PDOM", "39s",
            "NM", finish="FO", price=500, mtgjson_id="urza-printing",
        ),
        "cheap": item("cheap", "cheap", "Cheap", "SET", "2", "NM", price=60),
    }

    preview = build_batched_competitor_preview(
        [urza, cheap],
        lambda cart, seller: {"cart": [
            {"inventory_id": "cheap", "quantity_selected": 1},
            {"inventory_id": "urza", "quantity_selected": 1},
        ]},
        lambda ids: [listings[value] for value in ids],
    )

    by_name = {row["name"]: row for row in preview["changes"]}
    assert by_name["Urza's Ruinous Blast"]["competitor_price"] == 500
    assert by_name["Urza's Ruinous Blast"]["target_price"] == 495
    assert by_name["Cheap"]["target_price"] == 65
    assert by_name["Cheap"]["floor_applied"] is True


def test_stale_or_missing_listing_holds():
    inventory = [item("ours", "p", "Card", "SET", "1", "NM")]
    stale = item("comp", "p", "Card", "SET", "1", "NM", price=200)
    stale["quantity"] = 0

    preview = build_batched_competitor_preview(
        inventory,
        lambda cart, seller: {"cart": [{"inventory_id": "comp", "quantity_selected": 1}]},
        lambda ids: [stale],
    )

    assert preview["holds"][0]["action"] == "hold"
    assert "stale" in preview["holds"][0]["validation_reason"]


def test_progress_reports_optimizer_and_listing_completion():
    inventory = [item("ours", "p", "Card", "SET", "1", "NM")]
    competitor = item("comp", "p", "Card", "SET", "1", "NM", price=200)
    updates = []

    build_batched_competitor_preview(
        inventory,
        lambda cart, seller: {
            "cart": [{"inventory_id": "comp", "quantity_selected": 1}],
        },
        lambda ids: [competitor],
        progress_callback=updates.append,
    )

    assert updates[0]["stage"] == "optimizer"
    assert any(update["stage"] == "listing_resolution" for update in updates)
    assert updates[-1]["stage"] == "complete"
    assert updates[-1]["optimizer_batches_completed"] == 1
    assert updates[-1]["listing_chunks_completed"] == 1


def test_optimizer_failure_splits_then_holds_only_failing_single():
    inventory = [
        item("ours-a", "a", "Alpha", "ONE", "1", "NM"),
        item("ours-b", "b", "Beta", "TWO", "2", "NM"),
    ]
    competitor = item("comp-a", "a", "Alpha", "ONE", "1", "NM", price=200)

    def optimize(cart, seller):
        if len(cart) > 1 or cart[0]["name"] == "Beta":
            raise TimeoutError("simulated")
        return {"cart": [{"inventory_id": "comp-a", "quantity_selected": 1}]}

    preview = build_batched_competitor_preview(
        inventory,
        optimize,
        lambda ids: [competitor],
        batch_limit=20,
    )

    by_name = {row["name"]: row for row in preview["audit_rows"]}
    assert by_name["Alpha"]["action"] == "increase"
    assert by_name["Beta"]["action"] == "hold"
    assert by_name["Beta"]["validation_reason"] == "Optimizer request failed: TimeoutError"
    assert preview["summary"]["optimizer_calls"] == 3
    assert preview["summary"]["optimizer_failures"] == 2
