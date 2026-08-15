import json
from types import SimpleNamespace

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
