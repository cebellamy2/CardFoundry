from pricing_decision_service import (
    competitor_decision, market_decision, market_evidence_from_catalog,
)


def identity(finish="NF"):
    return {"name":"Card","set_code":"SET","collector_number":"1","scryfall_id":"sf","finish_id":finish}


def catalog(**overrides):
    row={"name":"Card","set_code":"SET","number":"1","scryfall_id":"sf","price_market":65,"price_market_foil":50}
    row.update(overrides)
    return {"meta":{"as_of":"2026-08-13T00:00:00Z"},"data":[row]}


def test_competitor_undercut_and_floor_classifications():
    assert competitor_decision({"price_cents":100},5,65)["target_price_cents"]==95
    low=competitor_decision({"price_cents":25},5,65)
    assert (low["target_price_cents"],low["classification"])==(65,"floor_limited_competitor")


def test_market_direct_floor_and_exact_floor():
    assert market_decision({"price_cents":80},65)["target_price_cents"]==80
    assert market_decision({"price_cents":20},65)["classification"]=="floor_limited_market"
    assert market_decision({"price_cents":65},65)["classification"]=="market_price_fallback"


def test_no_market_holds_and_evidence_changes_fail_stale_comparison():
    assert market_decision(None,65)["classification"]=="hold_no_price_evidence"
    assert market_decision({"price_cents":80},65)["evidence_hash"] != market_decision({"price_cents":81},65)["evidence_hash"]


def test_market_requires_exact_printing_and_finish_specific_field():
    assert market_evidence_from_catalog(identity(),catalog(set_code="OTHER")) is None
    assert market_evidence_from_catalog(identity("EF"),catalog()) is None
    assert market_evidence_from_catalog(identity("FO"),catalog())["price_cents"]==50


def test_market_evidence_is_auditable_and_condition_language_agnostic():
    evidence=market_evidence_from_catalog(identity(),catalog())
    assert evidence["source_endpoint"]=="/products/singles"
    assert evidence["source_field"]=="price_market"
    assert evidence["language_specific"] is False
    assert evidence["condition_specific"] is False
