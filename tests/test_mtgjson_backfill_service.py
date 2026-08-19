import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, RemoteProductBinding
from mtgjson_backfill_service import (
    MtgjsonOverrideError,
    build_mtgjson_backfill_preview,
    confirm_mtgjson_override,
    filter_preview_to_ready,
    run_additive_mtgjson_backfill,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'backfill.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_candidate(session, card_id=1):
    batch = session.query(Batch).first()
    if not batch:
        batch = Batch(batch_code="PROD", is_archived=False)
        session.add(batch); session.flush()
    card = InventoryCard(
        id=card_id, batch_id=batch.id, name="Alpha", set_code="ONE",
        collector_number="1", scryfall_id="sf-alpha", mtgjson_id=None,
        language_id="EN", condition_id="LP", finish_id="NF",
        condition="LP", finish="normal", status="available",
    )
    session.add(card); session.flush()
    return card


def add_binding(session, card_id=1, product_id="product-alpha", status="validated",
                evidence_hash="binding-hash"):
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps([card_id]),
        requested_identity_json=json.dumps({
            "name":"Alpha", "set_code":"ONE", "collector_number":"1",
            "scryfall_id":"sf-alpha", "language_id":"EN",
            "condition_id":"LP", "finish_id":"NF",
        }),
        scryfall_id="sf-alpha", mtgjson_id=None, language_id="EN",
        condition_id="LP", finish_id="NF", set_code="ONE", collector_number="1",
        binding_status=status, validated_at=datetime(2026,8,14), catalog_as_of="catalog-old",
        evidence_hash=evidence_hash, evidence_json="{}",
    )
    session.add(binding); session.flush()
    return binding


def seller(mtgjson="mtg-alpha", **single_overrides):
    single = {
        "name":"Alpha", "set":"ONE", "number":"1", "scryfall_id":"sf-alpha",
        "mtgjson_id":mtgjson, "language_id":"EN", "condition_id":"LP",
        "finish_id":"NF",
    }
    single.update(single_overrides)
    return {"id":"inventory-alpha", "product_id":"product-alpha",
            "product_type":"mtg_single", "effective_as_of":"seller-now",
            "product":{"single":single}}


def catalog(card_id="mtg-alpha"):
    return [{
        "card_id":card_id, "name":"Alpha", "set_code":"ONE", "number":"1",
        "scryfall_id":"sf-alpha", "variants":[{
            "product_id":"product-alpha", "language_id":"EN",
            "condition_id":"LP", "finish_id":"NF",
        }],
    }]


def classify(db, seller_rows, catalog_rows=None, bindings=1):
    with Session(db) as session:
        add_candidate(session)
        for index in range(bindings):
            add_binding(session, product_id=f"product-alpha{index or ''}",
                        evidence_hash=f"binding-{index}")
        session.commit()
        before = (session.query(InventoryCard).count(), session.query(RemoteProductBinding).count())
        result = build_mtgjson_backfill_preview(session, seller_rows, catalog_rows or [])
        after = (session.query(InventoryCard).count(), session.query(RemoteProductBinding).count())
        assert before == after
        assert session.get(InventoryCard, 1).mtgjson_id is None
        return result["rows"][0], result


def test_ready_uses_documented_seller_mtgjson_and_catalog_only_corrobates(db):
    row, result = classify(db, [seller()], catalog())
    assert row["classification"] == "ready"
    assert row["proposed_mtgjson_id"] == "mtg-alpha"
    assert row["identity_source"] == "seller_single_mtgjson_id"
    assert row["catalog_corroboration"] == "match"
    assert row["seller_evidence_hash"] and row["binding_evidence_hash"]
    assert result["preview_only"] is True and result["evidence_hash"]


def test_missing_documented_mtgjson(db):
    row, _ = classify(db, [seller(mtgjson=None)], [])
    assert row["classification"] == "missing_documented_mtgjson"
    assert row["proposed_mtgjson_id"] is None


def test_identity_conflict(db):
    row, _ = classify(db, [seller(set="TWO")], catalog())
    assert row["classification"] == "identity_conflict"
    assert "set_code" in row["reason"]


def test_ambiguous(db):
    row, _ = classify(db, [seller(), {**seller(), "id":"inventory-duplicate"}], catalog())
    assert row["classification"] == "ambiguous"


def test_binding_invalid(db):
    row, _ = classify(db, [seller()], catalog(), bindings=0)
    assert row["classification"] == "binding_invalid"


def test_exact_catalog_card_id_is_approved_only_as_legacy_backfill_source(db):
    row, _ = classify(db, [seller(mtgjson=None)], catalog("catalog-only-value"))
    assert row["classification"] == "ready"
    assert row["proposed_mtgjson_id"] == "catalog-only-value"
    assert row["identity_source"] == "catalog_card_id_legacy_backfill"


def test_confirm_mtgjson_override_sets_note_and_timestamp(db):
    with Session(db) as session:
        binding = add_binding(session)
        session.commit()
        binding_id = binding.id

        confirmed = confirm_mtgjson_override(session, binding_id, "Japanese foil, no MTGJSON documented")
        session.commit()
        assert confirmed.mtgjson_override_note == "Japanese foil, no MTGJSON documented"
        assert confirmed.mtgjson_override_confirmed_at is not None

        reloaded = session.get(RemoteProductBinding, binding_id)
        assert reloaded.mtgjson_override_confirmed_at is not None


def test_confirm_mtgjson_override_requires_a_note(db):
    with Session(db) as session:
        binding = add_binding(session)
        session.commit()
        with pytest.raises(MtgjsonOverrideError, match="reason is required"):
            confirm_mtgjson_override(session, binding.id, "   ")


def test_confirm_mtgjson_override_rejects_unknown_binding(db):
    with Session(db) as session:
        with pytest.raises(MtgjsonOverrideError, match="not found"):
            confirm_mtgjson_override(session, 999, "note")


def test_confirm_mtgjson_override_rejects_non_validated_binding(db):
    with Session(db) as session:
        binding = add_binding(session, status="deferred")
        session.commit()
        with pytest.raises(MtgjsonOverrideError, match="not a validated binding"):
            confirm_mtgjson_override(session, binding.id, "note")


def test_confirm_mtgjson_override_rejects_binding_that_already_has_mtgjson(db):
    with Session(db) as session:
        binding = add_binding(session)
        binding.mtgjson_id = "mtg-alpha"
        session.commit()
        with pytest.raises(MtgjsonOverrideError, match="already has a documented MTGJSON"):
            confirm_mtgjson_override(session, binding.id, "note")


def test_confirm_mtgjson_override_rejects_double_confirmation(db):
    with Session(db) as session:
        binding = add_binding(session)
        session.commit()
        confirm_mtgjson_override(session, binding.id, "first")
        session.commit()
        with pytest.raises(MtgjsonOverrideError, match="already overridden"):
            confirm_mtgjson_override(session, binding.id, "second")


def test_catalog_fallback_requires_exact_variant_identity(db):
    wrong = catalog("catalog-only-value")
    wrong[0]["variants"][0]["language_id"] = "JA"
    row, _ = classify(db, [seller(mtgjson=None)], wrong)
    assert row["classification"] == "identity_conflict"
    assert "source.language_id" in row["reason"]


def add_second_candidate(session, card_id=2, product_id="product-beta"):
    batch = session.query(Batch).first()
    card = InventoryCard(
        id=card_id, batch_id=batch.id, name="Beta", set_code="TWO",
        collector_number="2", scryfall_id="sf-beta", mtgjson_id=None,
        language_id="EN", condition_id="LP", finish_id="NF",
        condition="LP", finish="normal", status="available",
    )
    session.add(card)
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps([card_id]),
        requested_identity_json=json.dumps({
            "name": "Beta", "set_code": "TWO", "collector_number": "2",
            "scryfall_id": "sf-beta", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        }),
        scryfall_id="sf-beta", mtgjson_id=None, language_id="EN",
        condition_id="LP", finish_id="NF", set_code="TWO", collector_number="2",
        binding_status="validated", validated_at=datetime(2026, 8, 14),
        catalog_as_of="catalog-old", evidence_hash="binding-beta", evidence_json="{}",
    ))
    session.flush()
    return card


def test_filter_preview_to_ready_narrows_and_rehashes(db):
    with Session(db) as session:
        add_candidate(session)
        add_binding(session)
        add_second_candidate(session)
        session.commit()
        preview = build_mtgjson_backfill_preview(
            session,
            [seller(), {**seller(mtgjson=None), "id": "inventory-beta", "product_id": "product-beta",
                       "product": {"single": {**seller()["product"]["single"], "name": "Beta",
                                                "set": "TWO", "number": "2", "scryfall_id": "sf-beta",
                                                "mtgjson_id": None}}}],
            catalog(),
        )
        assert {row["classification"] for row in preview["rows"]} == {
            "ready", "missing_documented_mtgjson",
        }
        narrowed = filter_preview_to_ready(preview)
        assert len(narrowed["rows"]) == 1
        assert narrowed["rows"][0]["classification"] == "ready"
        assert narrowed["candidate_ids"] == [narrowed["rows"][0]["inventory_card_id"]]
        assert narrowed["summary"] == {
            "total_candidates": 1, "ready": 1, "missing_documented_mtgjson": 0,
            "identity_conflict": 0, "ambiguous": 0, "binding_invalid": 0,
        }
        # The narrowed evidence_hash must be self-consistent (execute_mtgjson_backfill
        # recomputes it the same way to verify "reviewed" and "fresh" match).
        from mtgjson_backfill_service import _hash, _preview_evidence
        assert narrowed["evidence_hash"] == _hash(_preview_evidence(narrowed))


def test_run_additive_mtgjson_backfill_applies_ready_and_skips_rest(db):
    with Session(db) as session:
        add_candidate(session)
        add_binding(session)
        add_second_candidate(session)
        session.commit()

        def seller_loader(min_quantity):
            return [seller()]

        def catalog_loader(product_ids):
            return {"meta": {"as_of": "catalog-now"}, "data": catalog()}

        result = run_additive_mtgjson_backfill(session, seller_loader, catalog_loader)
        session.commit()

        assert result["updated_inventory_cards"] == 1
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["classification"] == "missing_documented_mtgjson"
        assert result["skipped"][0]["inventory_card_id"] == 2

    with Session(db) as session:
        assert session.get(InventoryCard, 1).mtgjson_id == "mtg-alpha"
        assert session.get(InventoryCard, 2).mtgjson_id is None


def test_run_additive_mtgjson_backfill_is_a_safe_noop_with_nothing_to_backfill(db):
    with Session(db) as session:
        result = run_additive_mtgjson_backfill(
            session, lambda min_quantity: [], lambda product_ids: {"meta": {}, "data": []},
        )
        session.commit()
    assert result == {"updated_inventory_cards": 0, "updated_bindings": 0, "skipped": []}
