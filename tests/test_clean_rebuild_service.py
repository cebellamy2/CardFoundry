import json
from types import SimpleNamespace

import pytest

from clean_rebuild_service import (
    REBUILD_CONFIRMATION, build_clean_rebuild_preview, execute_maintenance_rebuild,
    run_rebuild_steps, validate_rebuild_staleness,
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
    new=card(2,"Beta",canonical=False,set_code="TWO",collector_number="2",scryfall_id="sf-2",finish="foil")
    good=preview(cards=[card(1),new],bindings=[binding()],prices=[priced()])
    row=next(r for r in good["republish_rows"] if r["source_type"]=="validated_new_product_binding")
    assert row["publish_price_cents"]==95
    held=preview(cards=[card(1),new],bindings=[binding()],prices=[priced(status="hold")])
    assert any(r["inventory_card_id"]==2 for r in held["exclusions"])


def test_intentional_weak_card_is_held():
    result=preview(cards=[card(1),card(2,"Monstrous Vortex",canonical=False,scryfall_id=None,finish=None)])
    assert result["summary"]["excluded_physical_cards"]==1
    assert result["exclusions"][0]["reason"]=="Insufficient identity"


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


def test_maintenance_executor_is_hard_disabled():
    with pytest.raises(RuntimeError,match="disabled"): execute_maintenance_rebuild()
