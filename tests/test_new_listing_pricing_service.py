import json
from types import SimpleNamespace

from new_listing_pricing_service import price_initial_bindings


def binding(binding_id=1, **identity_overrides):
    identity = {"name":"Alpha","set_code":"ONE","collector_number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    identity.update(identity_overrides)
    return SimpleNamespace(id=binding_id, product_id=f"product-{binding_id}", requested_identity_json=json.dumps(identity))


def listing(condition="NM", price=70, **overrides):
    single={"name":"Alpha","set":"ONE","number":"1","scryfall_id":"sf-alpha","language_id":"EN","condition_id":condition,"finish_id":"FO"}
    single.update(overrides)
    return {"id":"competitor","product_id":"other","quantity":1,"price_cents":price,"product":{"single":single}}


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


def test_exact_language_finish_and_printing_are_required():
    result=price_initial_bindings([binding()],lambda cart,seller:{"cart":[{"inventory_id":"competitor","quantity_selected":1}]},lambda ids:[listing(language_id="JA")],"seller")
    assert result["results"][0]["status"]=="hold"
