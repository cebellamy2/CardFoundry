import json
from types import SimpleNamespace

import competitor_pricing_service
from manual_price_override_service import identity_hash
from new_listing_pricing_service import price_initial_bindings, price_new_listing_candidates


def binding(binding_id=1, **identity_overrides):
    identity = {"name":"Alpha","set_code":"ONE","collector_number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    identity.update(identity_overrides)
    return SimpleNamespace(id=binding_id, product_id=f"product-{binding_id}",
                           evidence_hash=f"binding-hash-{binding_id}",
                           requested_identity_json=json.dumps(identity))


def manual(binding_id=1, price=125):
    return SimpleNamespace(
        status="active", remote_product_binding_id=binding_id,
        product_id=f"product-{binding_id}", binding_evidence_hash=f"binding-hash-{binding_id}",
        identity_json=binding(binding_id).requested_identity_json,
        manual_price_cents=price, note="Reviewed fallback", evidence_hash="manual-hash",
    )


def listing(condition="NM", price=70, **overrides):
    single={"name":"Alpha","set":"ONE","number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":condition,"finish_id":"FO"}
    single.update(overrides)
    return {"id":"competitor","product_id":"other","quantity":1,"price_cents":price,"effective_as_of":"2026-08-13T00:00:00Z","product":{"single":single}}


def test_verified_initial_price_uses_undercut_and_floor():
    result=price_initial_bindings([binding()],lambda cart,seller:{"cart":[{"inventory_id":"competitor","quantity_selected":1}]},lambda ids:[listing()],"seller")
    assert result["summary"]["priced"]==1
    assert result["results"][0]["target_price_cents"]==65


def test_worse_condition_cannot_map_and_holds():
    result=price_initial_bindings([binding()],lambda cart,seller:{"cart":[{"inventory_id":"competitor","quantity_selected":1}]},lambda ids:[listing(condition="MP")],"seller")
    assert result["results"][0]["status"]=="hold"


def test_409_unsatisfied_request_holds_without_guessing():
    def optimizer(cart,seller): return {"cart":[],"_conflicts":[{"item":{"index":0}}]}
    result=price_initial_bindings([binding()],optimizer,lambda ids:[],"seller")
    assert result["summary"]["held"]==1
    assert result["results"][0]["target_price_cents"] is None


def test_competitor_language_is_intentionally_ignored_and_audited():
    result=price_initial_bindings([binding()],lambda cart,seller:{"cart":[{"inventory_id":"competitor","quantity_selected":1}]},lambda ids:[listing(language_id="JA")],"seller")
    assert result["results"][0]["status"]=="priced"
    assert result["results"][0]["competitor_language_id"]=="JA"


def test_exact_finish_and_printing_are_still_required():
    optimizer=lambda cart,seller:{"cart":[{"inventory_id":"competitor","quantity_selected":1}]}
    assert price_initial_bindings([binding()],optimizer,lambda ids:[listing(finish_id="NF")],"seller")["results"][0]["status"]=="hold"
    assert price_initial_bindings([binding()],optimizer,lambda ids:[listing(set="TWO")],"seller")["results"][0]["status"]=="hold"


def test_market_fallback_is_not_undercut():
    def optimizer(cart,seller): return {"cart":[],"_conflicts":[{"item":{"index":0}}]}
    catalog=lambda ids:{"meta":{"as_of":"now"},"data":[{"name":"Alpha","set_code":"ONE","number":"1","scryfall_id":"sf-alpha","price_market_foil":80}]}
    row=price_initial_bindings([binding()],optimizer,lambda ids:[],"seller",market_catalog_call=catalog)["results"][0]
    assert row["target_price_cents"]==80
    assert row["price_classification"]=="market_price_fallback"


def test_manual_override_is_last_fallback_and_auditable():
    optimizer=lambda cart,seller:{"cart":[],"_conflicts":[{"item":{"index":0}}]}
    catalog=lambda ids:{"meta":{"as_of":"now"},"data":[{"name":"Alpha","set_code":"ONE","number":"1","scryfall_id":"sf-alpha","price_market_foil":None}]}
    row=price_initial_bindings(
        [binding()],optimizer,lambda ids:[],"seller",market_catalog_call=catalog,
        manual_overrides=[manual()],
    )["results"][0]
    assert row["status"]=="priced" and row["target_price_cents"]==125
    assert row["price_classification"]=="manual_price_override"
    assert row["manual_evidence"]["note"]=="Reviewed fallback"


def test_current_competitor_supersedes_manual_override():
    row=price_initial_bindings(
        [binding()],lambda cart,seller:{"cart":[{"inventory_id":"competitor"}]},
        lambda ids:[listing(price=200)],"seller",manual_overrides=[manual()],
    )["results"][0]
    assert row["price_classification"]=="competitor_undercut"
    assert row["target_price_cents"]==195


def test_current_market_supersedes_manual_override():
    optimizer=lambda cart,seller:{"cart":[],"_conflicts":[{"item":{"index":0}}]}
    catalog=lambda ids:{"meta":{"as_of":"now"},"data":[{"name":"Alpha","set_code":"ONE","number":"1","scryfall_id":"sf-alpha","price_market_foil":180}]}
    row=price_initial_bindings(
        [binding()],optimizer,lambda ids:[],"seller",market_catalog_call=catalog,
        manual_overrides=[manual()],
    )["results"][0]
    assert row["price_classification"]=="market_price_fallback"
    assert row["target_price_cents"]==180


# --- price_new_listing_candidates: the scryfall_id path (no RemoteProductBinding) ---
# candidate() is defined once, further below (shared with the pre-existing
# tests there) -- both shapes were equivalent for these tests' purposes,
# but two definitions of the same name in one module is just confusing.

def identity_manual(price=125,**identity_overrides):
    identity = {"name":"Alpha","set_code":"ONE","collector_number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    identity.update(identity_overrides)
    return SimpleNamespace(
        status="active", identity_hash=identity_hash(identity),
        identity_json=json.dumps(identity), manual_price_cents=price,
        note="Reviewed fallback", evidence_hash="manual-hash",
    )


def test_scryfall_path_has_no_override_tier_by_default():
    optimizer=lambda cart,seller:{"cart":[],"_conflicts":[{"item":{"index":0}}]}
    row=price_new_listing_candidates([candidate()],optimizer,lambda ids:[],"seller")["results"][0]
    assert row["status"]=="hold"


def test_scryfall_path_manual_override_is_last_fallback_and_auditable():
    optimizer=lambda cart,seller:{"cart":[],"_conflicts":[{"item":{"index":0}}]}
    catalog=lambda ids:{"meta":{"as_of":"now"},"data":[{"name":"Alpha","set_code":"ONE","number":"1","scryfall_id":"sf-alpha","price_market_foil":None}]}
    row=price_new_listing_candidates(
        [candidate()],optimizer,lambda ids:[],"seller",market_catalog_call=catalog,
        manual_overrides=[identity_manual()],
    )["results"][0]
    assert row["status"]=="priced" and row["target_price_cents"]==125
    assert row["price_classification"]=="manual_price_override"
    assert row["manual_evidence"]["note"]=="Reviewed fallback"


def test_scryfall_path_current_competitor_supersedes_manual_override():
    row=price_new_listing_candidates(
        [candidate()],lambda cart,seller:{"cart":[{"inventory_id":"competitor"}]},
        lambda ids:[listing(price=200)],"seller",manual_overrides=[identity_manual()],
    )["results"][0]
    assert row["status"]=="priced" and row["price_source"]=="competitor"
    assert row["target_price_cents"]==195


def test_scryfall_path_override_does_not_match_a_different_card():
    optimizer=lambda cart,seller:{"cart":[],"_conflicts":[{"item":{"index":0}}]}
    row=price_new_listing_candidates(
        [candidate(scryfall_id="sf-different")],optimizer,lambda ids:[],"seller",
        manual_overrides=[identity_manual()],
    )["results"][0]
    assert row["status"]=="hold"


def candidate(key="alpha-key", **identity_overrides):
    identity = {"name":"Alpha","set_code":"ONE","collector_number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    identity.update(identity_overrides)
    return {"key": key, "identity": identity}


def test_new_listing_candidate_needs_no_binding_or_product_id():
    result = price_new_listing_candidates(
        [candidate()],
        lambda cart, seller: {"cart": [{"inventory_id": "competitor", "quantity_selected": 1}]},
        lambda ids: [listing()], "seller",
    )
    assert result["summary"]["priced"] == 1
    assert result["results"][0]["target_price_cents"] == 65
    assert result["results"][0]["key"] == "alpha-key"


def test_new_listing_candidate_market_fallback_uses_scryfall_catalog_call():
    optimizer = lambda cart, seller: {"cart": [], "_conflicts": [{"item": {"index": 0}}]}
    calls = []

    def catalog(scryfall_ids):
        calls.append(scryfall_ids)
        return {"meta": {"as_of": "now"}, "data": [{
            "name": "Alpha", "set_code": "ONE", "number": "1",
            "scryfall_id": "sf-alpha", "price_market_foil": 80,
        }]}

    row = price_new_listing_candidates(
        [candidate()], optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
    )["results"][0]
    assert row["target_price_cents"] == 80
    assert row["price_classification"] == "market_price_fallback"
    assert calls == [["sf-alpha"]]


def test_new_listing_candidate_holds_without_manual_override_tier():
    optimizer = lambda cart, seller: {"cart": [], "_conflicts": [{"item": {"index": 0}}]}
    catalog = lambda ids: {"meta": {"as_of": "now"}, "data": []}
    row = price_new_listing_candidates(
        [candidate()], optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
    )["results"][0]
    assert row["status"] == "hold"
    assert row["price_classification"] == "hold_no_price_evidence"


# --- batching: N candidates needing a market-catalog fallback share ONE
# call instead of firing one HTTP request per candidate -- Perform Sync's
# new-listing preview was making N calls for N candidates despite the
# endpoint accepting 100 ids/call, confirmed live at 59 calls in a single
# run. ---------------------------------------------------------------------

def test_multiple_bindings_share_one_batched_market_catalog_call():
    optimizer = lambda cart, seller: {
        "cart": [], "_conflicts": [{"item": {"index": i}} for i in range(len(cart))],
    }
    calls = []

    def catalog(product_ids):
        calls.append(list(product_ids))
        return {"meta": {"as_of": "now"}, "data": [
            {"name": "Alpha", "set_code": "ONE", "number": str(i), "scryfall_id": f"sf-{i}",
             "price_market_foil": 80 + i}
            for i in range(3)
        ]}

    bindings = [
        binding(binding_id=i, collector_number=str(i), scryfall_id=f"sf-{i}")
        for i in range(3)
    ]
    result = price_initial_bindings(
        bindings, optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
    )
    assert len(calls) == 1
    assert sorted(calls[0]) == ["product-0", "product-1", "product-2"]
    priced_by_target = sorted(row["target_price_cents"] for row in result["results"])
    assert priced_by_target == [80, 81, 82]
    assert all(row["status"] == "priced" for row in result["results"])


def test_multiple_candidates_share_one_batched_scryfall_catalog_call():
    optimizer = lambda cart, seller: {
        "cart": [], "_conflicts": [{"item": {"index": i}} for i in range(len(cart))],
    }
    calls = []

    def catalog(scryfall_ids):
        calls.append(list(scryfall_ids))
        return {"meta": {"as_of": "now"}, "data": [
            {"name": "Alpha", "set_code": "ONE", "number": str(i), "scryfall_id": f"sf-{i}",
             "price_market_foil": 80 + i}
            for i in range(3)
        ]}

    candidates = [
        candidate(key=f"key-{i}", collector_number=str(i), scryfall_id=f"sf-{i}")
        for i in range(3)
    ]
    result = price_new_listing_candidates(
        candidates, optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
    )
    assert len(calls) == 1
    assert sorted(calls[0]) == ["sf-0", "sf-1", "sf-2"]
    priced_by_target = sorted(row["target_price_cents"] for row in result["results"])
    assert priced_by_target == [80, 81, 82]
    assert all(row["status"] == "priced" for row in result["results"])


def test_market_catalog_call_chunks_at_the_hundred_id_endpoint_limit():
    optimizer = lambda cart, seller: {
        "cart": [], "_conflicts": [{"item": {"index": i}} for i in range(len(cart))],
    }
    calls = []

    def catalog(product_ids):
        calls.append(list(product_ids))
        return {"meta": {"as_of": "now"}, "data": []}

    bindings = [
        binding(binding_id=i, collector_number=str(i), scryfall_id=f"sf-{i}")
        for i in range(101)
    ]
    price_initial_bindings(
        bindings, optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
        batch_size=200,
    )
    assert len(calls) == 2
    assert len(calls[0]) == 100
    assert len(calls[1]) == 1


def test_candidate_with_no_scryfall_id_gets_no_market_data_even_from_a_shared_batch():
    """The pre-batching guard (only call the catalog when a scryfall_id is
    present) has to survive the batch -- a request with none must never
    see the shared payload, or it could spuriously match another
    candidate's product by name/set/collector_number alone."""
    optimizer = lambda cart, seller: {
        "cart": [], "_conflicts": [{"item": {"index": i}} for i in range(len(cart))],
    }
    calls = []

    def catalog(scryfall_ids):
        calls.append(list(scryfall_ids))
        return {"meta": {"as_of": "now"}, "data": [
            {"name": "Alpha", "set_code": "ONE", "number": "1", "scryfall_id": "sf-1",
             "price_market_foil": 80},
        ]}

    candidates = [
        candidate(key="key-0", collector_number="0", scryfall_id=""),
        candidate(key="key-1", collector_number="1", scryfall_id="sf-1"),
    ]
    result = price_new_listing_candidates(
        candidates, optimizer, lambda ids: [], "seller", market_catalog_call=catalog,
    )
    assert calls == [["sf-1"]]
    by_key = {row["key"]: row for row in result["results"]}
    assert by_key["key-0"]["status"] == "hold"
    assert by_key["key-1"]["status"] == "priced"
    assert by_key["key-1"]["target_price_cents"] == 80


# --- pacing: the gap this closes. Both scryfall_id and product_id new-
# listing pricing call the same rate-limited /buyer/optimizer endpoint
# Flow B does, but were unpaced -- confirmed live: 104 new-listing
# candidates fanned out with no spacing tripped the same rate limit Flow
# B used to, in a path v1.55.4's pacing fix never touched. -------------

class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _wire_fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(competitor_pricing_service.time, "sleep", clock.sleep)
    monkeypatch.setattr(competitor_pricing_service.time, "monotonic", lambda: clock.now)
    return clock


def test_scryfall_path_paces_between_optimizer_batches(monkeypatch):
    clock = _wire_fake_clock(monkeypatch)
    optimizer = lambda cart, seller: {"cart": []}
    candidates = [candidate(key=f"key-{i}", scryfall_id=f"sf-{i}") for i in range(4)]

    price_new_listing_candidates(
        candidates, optimizer, lambda ids: [], "seller",
        batch_size=1, min_request_interval=0.5,
    )

    assert clock.slept == [0.5, 0.5, 0.5]


def test_scryfall_path_without_pacing_makes_no_sleep_calls(monkeypatch):
    clock = _wire_fake_clock(monkeypatch)
    optimizer = lambda cart, seller: {"cart": []}

    price_new_listing_candidates(
        [candidate()], optimizer, lambda ids: [], "seller", min_request_interval=0,
    )

    assert clock.slept == []


def test_scryfall_path_default_pacing_reads_the_shared_flow_b_interval(monkeypatch):
    """Both paths hit the same account-level Mana Pool rate limit, so they
    share one budget/config rather than each tracking its own."""
    clock = _wire_fake_clock(monkeypatch)
    monkeypatch.setattr(
        competitor_pricing_service, "OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS", 0.5,
    )
    optimizer = lambda cart, seller: {"cart": []}
    candidates = [candidate(key=f"key-{i}", scryfall_id=f"sf-{i}") for i in range(2)]

    price_new_listing_candidates(candidates, optimizer, lambda ids: [], "seller", batch_size=1)

    assert clock.slept == [0.5]


def test_binding_path_paces_between_optimizer_batches(monkeypatch):
    clock = _wire_fake_clock(monkeypatch)
    optimizer = lambda cart, seller: {"cart": []}
    bindings = [binding(binding_id=i, scryfall_id=f"sf-{i}") for i in range(4)]

    price_initial_bindings(
        bindings, optimizer, lambda ids: [], "seller",
        batch_size=1, min_request_interval=0.5,
    )

    assert clock.slept == [0.5, 0.5, 0.5]


def test_binding_path_without_pacing_makes_no_sleep_calls(monkeypatch):
    clock = _wire_fake_clock(monkeypatch)
    optimizer = lambda cart, seller: {"cart": []}

    price_initial_bindings(
        [binding()], optimizer, lambda ids: [], "seller", min_request_interval=0,
    )

    assert clock.slept == []
