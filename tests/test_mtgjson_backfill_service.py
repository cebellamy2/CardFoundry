import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, RemoteProductBinding
from mtgjson_backfill_service import build_mtgjson_backfill_preview


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


def test_catalog_fallback_requires_exact_variant_identity(db):
    wrong = catalog("catalog-only-value")
    wrong[0]["variants"][0]["language_id"] = "JA"
    row, _ = classify(db, [seller(mtgjson=None)], wrong)
    assert row["classification"] == "identity_conflict"
    assert "source.language_id" in row["reason"]
