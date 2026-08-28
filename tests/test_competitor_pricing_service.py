import threading
import time

import httpx
import pytest

import competitor_pricing_service

from competitor_pricing_service import (
    DEFAULT_OPTIMIZER_BATCH_SIZE,
    OPTIMIZER_CONCURRENCY,
    CompetitorPricingError,
    apply_full_competitor_preview,
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


def _single_change_preview(ours_price=100, competitor_price=90):
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", price=ours_price)
    competitor = item("comp", "comp-product", "Card", "SET", "1", "LP", price=competitor_price)
    preview = build_batched_competitor_preview(
        [ours],
        lambda cart, seller: {"cart": [{"inventory_id": "comp", "quantity_selected": 1}]},
        lambda ids: [competitor],
    )
    return preview, competitor


def _fake_writer(responses_by_product_id=None):
    calls = []

    def writer(updates):
        calls.append(updates)
        inventory = [
            {
                "product_id": u["product_id"], "price_cents": u["price_cents"],
                "product": {"single": {"name": "Card"}},
            }
            for u in updates
        ]
        return [{"inventory": inventory, "skipped": []}]

    writer.calls = calls
    return writer


def test_apply_writes_fresh_target_when_competitor_unchanged():
    preview, competitor = _single_change_preview(ours_price=100, competitor_price=90)
    writer = _fake_writer()

    result = apply_full_competitor_preview(
        preview, {"ours-product"}, lambda ids: [competitor], writer, 5, 65,
    )

    assert result["updates"] == [{
        "product_type": "mtg_single", "product_id": "ours-product",
        "price_cents": 85, "quantity": None,
    }]
    assert not result["excluded"]
    assert not result["repriced"]
    assert writer.calls == [result["updates"]]


def test_apply_reprices_when_competitor_moved_within_tolerance():
    preview, competitor = _single_change_preview(ours_price=100, competitor_price=90)
    fresh_competitor = {**competitor, "price_cents": 92}
    writer = _fake_writer()

    result = apply_full_competitor_preview(
        preview, {"ours-product"}, lambda ids: [fresh_competitor], writer, 5, 65,
    )

    # reviewed target = 90-5 = 85; fresh target = 92-5 = 87; drift = 2/85 ~= 2.4% < 10%
    assert result["updates"][0]["price_cents"] == 87
    assert result["repriced"] == [
        {**preview["changes"][0], "reviewed_target_price": 85, "fresh_target_price": 87},
    ]


def test_apply_excludes_when_competitor_moved_beyond_tolerance():
    preview, competitor = _single_change_preview(ours_price=100, competitor_price=90)
    fresh_competitor = {**competitor, "price_cents": 200}
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [fresh_competitor], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_excludes_when_no_longer_locally_sellable():
    preview, competitor = _single_change_preview()
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, set(), lambda ids: [competitor], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_excludes_when_competitor_listing_no_longer_exists():
    preview, _ = _single_change_preview()
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_excludes_when_competitor_listing_sold_out():
    preview, competitor = _single_change_preview()
    fresh_competitor = {**competitor, "quantity": 0}
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [fresh_competitor], writer, 5, 65,
        )
    assert writer.calls == []


def _floor_repair_preview(price=15, floor_cents=65):
    # No competitor, no market fallback -- the only pricing basis is the
    # owner-configured floor itself (price_source == "owner_floor_policy").
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", price=price)
    preview = build_batched_competitor_preview(
        [ours], lambda payload, seller: {"_conflicts": [{"item": {"index": 0}}]},
        lambda ids: [], floor_cents=floor_cents,
    )
    return preview, ours


def test_apply_writes_floor_repair_with_no_competitor_or_market_basis():
    """Regression: a floor-repair row's competitor_inventory_id is always
    null (there was never a competitor basis), so routing it through the
    competitor-listing re-verification always failed closed with
    "Competitor listing no longer exists" -- every floor correction was
    silently discarded on every apply, forever. Confirmed live against
    production: 523 listings stuck below a $0.65 floor despite the
    scheduled pricing cron completing successfully every ~8 hours."""
    preview, ours = _floor_repair_preview(price=15)
    writer = _fake_writer()

    result = apply_full_competitor_preview(
        preview, {"ours-product"}, lambda ids: [ours], writer, 5, 65,
    )

    assert result["updates"] == [{
        "product_type": "mtg_single", "product_id": "ours-product",
        "price_cents": 65, "quantity": None,
    }]
    assert not result["excluded"]
    assert writer.calls == [result["updates"]]


def test_apply_excludes_floor_repair_when_already_fixed_since_preview():
    preview, ours = _floor_repair_preview(price=15)
    fresh_ours = {**ours, "price_cents": 65}
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [fresh_ours], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_excludes_floor_repair_when_no_longer_locally_sellable():
    preview, ours = _floor_repair_preview(price=15)
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, set(), lambda ids: [ours], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_excludes_floor_repair_when_listing_no_longer_exists():
    preview, ours = _floor_repair_preview(price=15)
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_raises_when_preview_has_no_changes():
    preview = {"changes": []}
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(preview, set(), lambda ids: [], writer, 5, 65)
    assert writer.calls == []


def test_apply_is_batch_isolated_across_rows():
    inventory = [
        item("ours-a", "a-lp", "Alpha", "ONE", "1", "LP", price=100),
        item("ours-b", "b-lp", "Beta", "TWO", "2", "LP", price=50),
    ]
    competitors = {
        "comp-a": item("comp-a", "a-comp", "Alpha", "ONE", "1", "LP", price=90),
        "comp-b": item("comp-b", "b-comp", "Beta", "TWO", "2", "LP", price=40),
    }

    def optimize(cart, seller_id):
        return {"cart": [
            {"inventory_id": "comp-a", "quantity_selected": 1},
            {"inventory_id": "comp-b", "quantity_selected": 1},
        ]}

    preview = build_batched_competitor_preview(
        inventory, optimize, lambda ids: [competitors[value] for value in ids],
    )

    # Only Alpha's competitor is still resolvable fresh -- Beta's disappeared
    # (e.g. sold out). Alpha must still apply.
    writer = _fake_writer()
    result = apply_full_competitor_preview(
        preview, {"a-lp", "b-lp"},
        lambda ids: [competitors["comp-a"]] if "comp-a" in ids else [],
        writer, 5, 65,
    )

    assert len(result["updates"]) == 1
    assert result["updates"][0]["product_id"] == "a-lp"
    assert len(result["excluded"]) == 1
    assert result["excluded"][0]["exclusion_reason"] == "Competitor listing no longer exists"


def _market_fallback_preview(ours_price=100, market_price=200):
    ours = item("ours", "ours-product", "Card", "SET", "1", "LP", price=ours_price)

    def optimize(cart, seller):
        return {"cart": [], "_conflicts": [{"item": {"index": 0}}]}

    def catalog(ids):
        return {"meta": {"as_of": "now"}, "data": [{
            "name": "Card", "set_code": "SET", "number": "1", "scryfall_id": "",
            "price_market": market_price, "variants": [{"product_id": "ours-product"}],
        }]}

    preview = build_batched_competitor_preview(
        [ours], optimize, lambda ids: [], market_catalog_call=catalog,
    )
    return preview


def test_apply_excludes_market_rows_when_no_catalog_loader_given():
    """Fail closed: a market-fallback row must never be written unverified
    just because the caller didn't supply a way to re-check it."""
    preview = _market_fallback_preview(ours_price=100, market_price=200)
    assert preview["changes"][0]["price_source"] == "market"
    writer = _fake_writer()

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [], writer, 5, 65,
        )
    assert writer.calls == []


def test_apply_reverifies_market_row_against_fresh_catalog():
    preview = _market_fallback_preview(ours_price=100, market_price=200)
    writer = _fake_writer()

    def fresh_catalog(ids):
        return {"meta": {"as_of": "later"}, "data": [{
            "name": "Card", "set_code": "SET", "number": "1", "scryfall_id": "",
            "price_market": 205, "variants": [{"product_id": "ours-product"}],
        }]}

    result = apply_full_competitor_preview(
        preview, {"ours-product"}, lambda ids: [], writer, 5, 65,
        market_catalog_loader=fresh_catalog,
    )

    assert result["updates"] == [{
        "product_type": "mtg_single", "product_id": "ours-product",
        "price_cents": 205, "quantity": None,
    }]
    assert result["repriced"][0]["fresh_target_price"] == 205


def test_apply_excludes_market_row_when_evidence_no_longer_trustworthy():
    preview = _market_fallback_preview(ours_price=100, market_price=200)
    writer = _fake_writer()

    # The fresh catalog no longer has this product at all (e.g. delisted) --
    # market_evidence_from_catalog finds zero matches, so there's no
    # trustworthy evidence left to price from.
    def empty_catalog(ids):
        return {"meta": {"as_of": "later"}, "data": []}

    with pytest.raises(CompetitorPricingError):
        apply_full_competitor_preview(
            preview, {"ours-product"}, lambda ids: [], writer, 5, 65,
            market_catalog_loader=empty_catalog,
        )
    assert writer.calls == []


def test_apply_handles_mixed_competitor_and_market_rows_independently():
    inventory = [
        item("ours-a", "a-lp", "Alpha", "ONE", "1", "LP", price=100),
        item("ours-b", "b-lp", "Beta", "TWO", "2", "LP", price=50),
    ]
    competitor = item("comp-a", "a-comp", "Alpha", "ONE", "1", "LP", price=90)

    def optimize(cart, seller):
        results = []
        for request in cart:
            if request["name"] == "Alpha":
                results.append({"inventory_id": "comp-a", "quantity_selected": 1})
        return {"cart": results, "_conflicts": (
            [] if len(results) == len(cart)
            else [{"item": {"index": i}} for i, r in enumerate(cart) if r["name"] != "Alpha"]
        )}

    def catalog(ids):
        return {"meta": {"as_of": "now"}, "data": [{
            "name": "Beta", "set_code": "TWO", "number": "2", "scryfall_id": "",
            "price_market": 80, "variants": [{"product_id": "b-lp"}],
        }]}

    preview = build_batched_competitor_preview(
        inventory, optimize, lambda ids: [competitor], market_catalog_call=catalog,
    )
    sources = {row["product_id"]: row["price_source"] for row in preview["changes"]}
    assert sources == {"a-lp": "competitor", "b-lp": "market"}

    writer = _fake_writer()
    result = apply_full_competitor_preview(
        preview, {"a-lp", "b-lp"}, lambda ids: [competitor], writer, 5, 65,
        market_catalog_loader=catalog,
    )

    written_products = {u["product_id"] for u in result["updates"]}
    assert written_products == {"a-lp", "b-lp"}
    assert not result["excluded"]


def _rate_limit_error():
    """The exception optimize_exact_variant_batch_with_conflicts raises once
    manapool_service's retry gives up on a 429 (response.raise_for_status)."""
    request = httpx.Request("POST", "https://manapool.com/api/v1/buyer/optimizer")
    response = httpx.Response(
        429, json={"status": 429, "message": "Rate limit exceeded"}, request=request,
    )
    return httpx.HTTPStatusError("429", request=request, response=response)


def test_rate_limited_batch_holds_without_bisecting():
    """Splitting a rate-limited batch sends more requests to the limiter that
    just refused us -- live, that turned 264 batches into thousands of doomed
    calls and blew the scheduled cron's poll timeout. One call, then hold."""
    inventory = [
        item("ours-a", "a", "Alpha", "ONE", "1", "NM"),
        item("ours-b", "b", "Beta", "TWO", "2", "NM"),
        item("ours-c", "c", "Gamma", "THR", "3", "NM"),
    ]

    calls = []

    def optimize(cart, seller):
        calls.append(len(cart))
        raise _rate_limit_error()

    preview = build_batched_competitor_preview(
        inventory, optimize, lambda ids: [], batch_limit=20,
    )

    assert calls == [3]
    assert preview["summary"]["optimizer_calls"] == 1
    by_name = {row["name"]: row for row in preview["audit_rows"]}
    for name in ("Alpha", "Beta", "Gamma"):
        assert by_name[name]["action"] == "hold"
        assert by_name[name]["validation_reason"] == (
            "Mana Pool rate limit still closed; not priced this run"
        )


def test_non_rate_limit_failure_still_bisects():
    """Bisection is how a batch too large for the optimizer gets narrowed to
    the single request that fails -- only 429s opt out of it."""
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
        inventory, optimize, lambda ids: [competitor], batch_limit=20,
    )

    by_name = {row["name"]: row for row in preview["audit_rows"]}
    assert by_name["Alpha"]["action"] == "increase"
    assert by_name["Beta"]["validation_reason"] == "Optimizer request failed: TimeoutError"


class _FakeClock:
    """Monotonic clock a test drives by hand, so pacing assertions don't
    depend on real elapsed time."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _pacer(interval):
    clock = _FakeClock()
    pacer = competitor_pricing_service._RequestPacer(
        interval, sleep=clock.sleep, now=lambda: clock.now,
    )
    return pacer, clock


def test_pacer_lets_the_first_request_through_immediately():
    pacer, clock = _pacer(1.0)

    assert pacer.wait() == 0.0
    assert clock.slept == []


def test_pacer_spaces_successive_requests_by_the_interval():
    pacer, clock = _pacer(1.0)

    pacer.wait()
    assert pacer.wait() == 1.0
    assert pacer.wait() == 1.0
    assert clock.slept == [1.0, 1.0]


def test_pacer_does_not_delay_a_request_that_arrives_after_its_slot():
    """A slow optimizer call already paid the interval -- pacing must not
    charge for it twice."""
    pacer, clock = _pacer(1.0)

    pacer.wait()
    clock.now += 5.0

    assert pacer.wait() == 0.0
    assert clock.slept == []


def test_pacer_interval_of_zero_disables_pacing():
    pacer, clock = _pacer(0)

    for _ in range(5):
        assert pacer.wait() == 0.0
    assert clock.slept == []


def test_preview_paces_between_optimizer_batches(monkeypatch):
    """The burst that tripped the live rate limit: many batches fanned out
    with nothing spacing the requests apart."""
    # Both sleep and the clock come from the fake, so a reserved slot is
    # actually reached by sleeping to it -- with a fake sleep alone, real
    # time keeps moving and each reservation drifts further out.
    clock = _FakeClock()
    monkeypatch.setattr(competitor_pricing_service.time, "sleep", clock.sleep)
    monkeypatch.setattr(competitor_pricing_service.time, "monotonic", lambda: clock.now)

    inventory = [
        item(f"ours-{i}", f"p{i}", f"Card{i}", "SET", str(i), "NM")
        for i in range(4)
    ]

    build_batched_competitor_preview(
        inventory,
        lambda cart, seller: {"cart": []},
        lambda ids: [],
        batch_limit=1,
        min_request_interval=0.5,
    )

    # Four batches, so three gaps -- the first request is never delayed.
    assert clock.slept == [0.5, 0.5, 0.5]
    assert clock.now == pytest.approx(1.5)


def test_preview_without_pacing_makes_no_sleep_calls(monkeypatch):
    slept = []
    monkeypatch.setattr(competitor_pricing_service.time, "sleep", slept.append)

    build_batched_competitor_preview(
        [item("ours", "p", "Card", "SET", "1", "NM")],
        lambda cart, seller: {"cart": []},
        lambda ids: [],
        min_request_interval=0,
    )

    assert slept == []
