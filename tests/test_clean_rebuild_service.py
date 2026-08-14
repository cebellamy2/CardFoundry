import json
from types import SimpleNamespace

import pytest

from clean_rebuild_service import (
    REBUILD_CONFIRMATION, build_clean_rebuild_preview, execute_maintenance_rebuild,
    run_rebuild_steps, validate_rebuild_staleness,
)
from clean_rebuild_workflow import (
    _catalog_identity_proxies, _resolve_missing_canonical_products,
)


def card(card_id, name="Alpha", canonical=True, **overrides):
    values = {
        "id": card_id, "batch_id": 1, "name": name, "set_code": "ONE",
        "collector_number": "1", "scryfall_id": f"sf-{card_id}",
        "mtgjson_id": "mtg-alpha" if canonical else None,
        "language_id": "EN" if canonical else None,
        "condition_id": "LP" if canonical else None,
        "finish_id": "NF" if canonical else None,
        "condition": "near_mint", "finish": "normal", "status": "available",
    }
    values.update(overrides); return SimpleNamespace(**values)


def remote(product="existing", quantity=1, price=100, identity=True, inventory=None):
    single = {"name":"Alpha","set":"ONE","number":"1","scryfall_id":"sf-1","mtgjson_id":"mtg-alpha","language_id":"EN","condition_id":"LP","finish_id":"NF"} if identity else {}
    return {"id":inventory or f"inventory-{product}","product_id":product,"product_type":"mtg_single","quantity":quantity,"price_cents":price,"effective_as_of":"now","product":{"single":single}}


def binding(card_ids=(2,), binding_id=1, product="new"):
    identity={"name":"Beta","set_code":"TWO","collector_number":"2","scryfall_id":"sf-2","language_id":"EN","condition_id":"LP","finish_id":"FO"}
    return SimpleNamespace(id=binding_id,binding_status="validated",product_id=product,evidence_hash="binding-hash",local_card_ids_json=json.dumps(list(card_ids)),requested_identity_json=json.dumps(identity))


def priced(binding_id=1, status="priced"):
    return {"binding_id":binding_id,"status":status,"reason":"no competitor" if status=="hold" else "ok","target_price_cents":95 if status=="priced" else None,"evidence_hash":"price-hash"}


def preview(cards=None, remotes=None, bindings=(), prices=()):
    return build_clean_rebuild_preview(
        cards or [card(1)], {1:SimpleNamespace(is_archived=False)}, [],
        remotes or [remote()], list(bindings), {"results":list(prices)},
    )


def test_blank_includes_remote_only_positive_and_skips_zero_history():
    result=preview(remotes=[remote(),remote("remote-only",3,identity=False),remote("zero",0,identity=False)])
    assert result["summary"]["positive_listings_to_blank"]==2
    assert result["summary"]["remote_copies_to_blank"]==4
    assert result["summary"]["remote_only_positive_included"]==1
    assert all(row["product_id"]!="zero" for row in result["blank_rows"])


def test_exact_local_total_and_existing_price_preservation():
    result=preview(cards=[card(1),card(2)],remotes=[remote(quantity=2)])
    assert result["summary"]["copies_to_republish"]==2
    assert result["summary"]["quantity_accounting_matches"] is True
    row=result["republish_rows"][0]
    assert row["publish_price_behavior"]=="preserve_with_null"
    assert result["republish_payloads"][0][0]["price_cents"] is None


def test_net_new_uses_verified_price_and_held_price_is_excluded():
    new=card(2,"Beta",set_code="TWO",collector_number="2",scryfall_id="sf-2",
             mtgjson_id="mtg-beta",finish_id="FO",finish="foil")
    good=preview(cards=[card(1),new],bindings=[binding()],prices=[priced()])
    row=next(r for r in good["republish_rows"] if r["source_type"]=="validated_remote_binding")
    assert row["publish_price_cents"]==95
    held=preview(cards=[card(1),new],bindings=[binding()],prices=[priced(status="hold")])
    assert any(r["inventory_card_id"]==2 for r in held["exclusions"])


def test_validated_binding_without_mtgjson_is_not_publishable():
    bound_only=card(
        2,"Beta",canonical=False,set_code="TWO",collector_number="2",
        scryfall_id="sf-2",language_id="EN",condition_id="LP",finish_id="FO",
        finish="foil",
    )
    with pytest.raises(ValueError, match="MTGJSON identity: 2"):
        preview(cards=[card(1),bound_only],bindings=[binding()],prices=[priced()])


def test_manual_fallback_evidence_makes_exact_bound_variant_publishable():
    new=card(2,"Beta",set_code="TWO",collector_number="2",scryfall_id="sf-2",
             mtgjson_id="mtg-beta",finish_id="FO",finish="foil")
    manual={**priced(),"price_classification":"manual_price_override","price_source":"manual"}
    result=preview(cards=[card(1),new],bindings=[binding()],prices=[manual])
    row=next(r for r in result["republish_rows"] if r["product_id"]=="new")
    assert row["publish_price_cents"]==95
    assert row["pricing_classification"]=="manual_price_override"
    assert result["summary"]["quantity_accounting_matches"] is True


def test_validated_binding_with_seller_history_preserves_existing_price():
    new=card(2,"Beta",set_code="TWO",collector_number="2",scryfall_id="sf-2",
             mtgjson_id="mtg-beta",finish_id="FO",finish="foil")
    history=remote("new",0,175,identity=False)
    result=preview(cards=[card(1),new],remotes=[remote(),history],bindings=[binding()],prices=[])
    row=next(r for r in result["republish_rows"] if r["product_id"]=="new")
    assert row["source_type"]=="validated_remote_binding"
    assert row["publish_price_behavior"]=="preserve_with_null"
    assert row["publish_price_cents"] is None
    assert row["existing_remote_inventory_id"]=="inventory-new"


def test_intentional_weak_card_cannot_bypass_canonical_preflight():
    with pytest.raises(ValueError, match="MTGJSON identity: 2"):
        preview(cards=[
            card(1),
            card(2,"Monstrous Vortex",canonical=False,scryfall_id=None,finish=None),
        ])


@pytest.mark.parametrize("field,message",[
    ("local_snapshot_hash","Local inventory changed"),
    ("remote_snapshot_hash","Seller inventory changed"),
    ("binding_evidence_hashes","Remote binding evidence changed"),
    ("initial_price_evidence","Initial price evidence changed"),
])
def test_all_stale_evidence_aborts(field,message):
    reviewed=preview(); fresh=dict(reviewed); fresh[field]=["changed"]
    with pytest.raises(ValueError,match=message): validate_rebuild_staleness(reviewed,fresh,REBUILD_CONFIRMATION)


def test_confirmation_enforced():
    result=preview()
    with pytest.raises(ValueError,match="confirmation"): validate_rebuild_staleness(result,result,"STORE ON")


def test_blank_verification_failure_prevents_republish():
    result=preview(); writes=[]
    with pytest.raises(RuntimeError,match="republish prohibited"):
        run_rebuild_steps(result,lambda payload:writes.append(payload),lambda min_quantity:[remote(quantity=1)])
    assert len(writes)==1


def test_republish_mismatch_fails_using_seller_inventory_only():
    result=preview(); reads=iter([[remote(quantity=0)],[remote(quantity=0)]])
    with pytest.raises(RuntimeError,match="quantity mismatch"):
        run_rebuild_steps(result,lambda payload:None,lambda min_quantity:next(reads))


def test_successful_reconciliation_never_needs_buyer_endpoint():
    result=preview(); reads=iter([[remote(quantity=0)],[remote(quantity=1)]])
    assert run_rebuild_steps(result,lambda payload:None,lambda min_quantity:next(reads))["status"]=="reconciled"


def test_legacy_unsealed_executor_entry_remains_refused():
    with pytest.raises(RuntimeError, match="separate approval"):
        execute_maintenance_rebuild()


def test_missing_canonical_seller_variant_gets_preview_only_catalog_binding():
    local=card(7,name="Gamma",set_code="THREE",collector_number="7",
               scryfall_id="sf-7",mtgjson_id="mtg-gamma")
    def lookup(ids,languages=None):
        return {"meta":{"as_of":"now"},"data":[{
            "name":"Gamma","set_code":"THREE","number":"7","scryfall_id":"sf-7",
            "variants":[{"product_type":"mtg_single","product_id":"gamma-lp",
                         "language_id":"EN","condition_id":"LP","finish_id":"NF"}],
        }]}
    bindings,evidence=_resolve_missing_canonical_products([local],lookup)
    assert len(bindings)==1
    assert bindings[0].product_id=="gamma-lp"
    assert json.loads(bindings[0].local_card_ids_json)==[7]
    assert evidence[0]["validation_status"]=="validated"


def test_localized_scryfall_uses_unique_seller_printing_family_catalog_id():
    local=card(8,name="Localized",set_code="STA",collector_number="8",
               scryfall_id="localized-ja",language_id="JA",mtgjson_id="mtg-ja")
    seller=remote("ja-nm",0,100,inventory="history")
    seller["product"]["single"].update({
        "name":"Localized","set":"STA","number":"8","scryfall_id":"shared-catalog",
        "mtgjson_id":"mtg-ja","language_id":"JA","condition_id":"NM","finish_id":"NF",
    })
    proxy=_catalog_identity_proxies([local],[seller])[0]
    assert proxy.scryfall_id=="localized-ja"
    assert proxy.catalog_scryfall_id=="shared-catalog"
