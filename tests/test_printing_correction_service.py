import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, InventoryChangeLog, RemoteProductBinding
from printing_correction_service import (
    PrintingCorrectionError,
    apply_printing_correction,
    build_printing_correction_preview,
)
import main


OLD_SCRYFALL = "ded66cfb-d1fa-4dbb-af19-8eeba6fbf86d"
NEW_SCRYFALL = "0634ab23-4691-4c77-9b8f-bfd9d99b31a1"


def scryfall_lookup(ids):
    return {NEW_SCRYFALL: {
        "id": NEW_SCRYFALL, "name": "Library of Leng", "set": "3ed",
        "collector_number": "261", "lang": "en", "finishes": ["nonfoil"],
    }}


def catalog_lookup(ids, languages=None):
    assert ids == [NEW_SCRYFALL]
    assert languages == ["EN"]
    return {"meta": {"as_of": "catalog-now"}, "data": [{
        "name": "Library of Leng", "set_code": "3ED", "number": "261",
        "scryfall_id": NEW_SCRYFALL, "variants": [{
            "product_type": "mtg_single", "product_id": "revised-lp",
            "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        }],
    }]}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correction.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch); session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Library of Leng", set_code="SUM",
            collector_number="261", scryfall_id=OLD_SCRYFALL,
            language_id="EN", condition="LP", condition_id="LP",
            finish="normal", finish_id="NF", status="available",
        )
        session.add(card); session.flush()
        evidence = {"inventory_card_ids": [card.id], "product_id": "summer-lp"}
        session.add(RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id="summer-lp",
            local_card_ids_json=json.dumps([card.id]),
            requested_identity_json=json.dumps({"scryfall_id": OLD_SCRYFALL}),
            scryfall_id=OLD_SCRYFALL, mtgjson_id=None, language_id="EN",
            condition_id="LP", finish_id="NF", set_code="SUM",
            collector_number="261", binding_status="validated",
            validated_at=datetime.now(), catalog_as_of="old",
            evidence_hash="old-evidence", evidence_json=json.dumps(evidence),
        ))
        session.commit()
    return engine


def preview(session, card):
    return build_printing_correction_preview(
        session, card, NEW_SCRYFALL, [], catalog_lookup, scryfall_lookup,
    )


def test_preview_is_read_only_and_resolves_exact_revised_product(db):
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        result = preview(session, card)
        assert result["preview_only"] is True
        assert result["card_after"]["set_code"] == "3ED"
        assert result["card_after"]["scryfall_id"] == NEW_SCRYFALL
        assert result["resolution"]["product_id"] == "revised-lp"
        assert result["old_binding_ids"]
        assert card.set_code == "SUM"
        assert session.query(RemoteProductBinding).one().product_id == "summer-lp"


def test_confirm_atomically_replaces_binding_and_audits(db):
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        reviewed = preview(session, card)
        current = preview(session, card)
        apply_printing_correction(session, card, reviewed, current)
        session.commit()
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        binding = session.query(RemoteProductBinding).one()
        assert (card.set_code, card.collector_number, card.scryfall_id) == (
            "3ED", "261", NEW_SCRYFALL,
        )
        assert binding.product_id == "revised-lp"
        assert json.loads(binding.local_card_ids_json) == [card.id]
        assert session.query(InventoryChangeLog).count() == 1


def test_stale_preview_refuses_without_changes(db):
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        reviewed = preview(session, card)
        card.collector_number = "999"
        current = preview(session, card)
        with pytest.raises(PrintingCorrectionError, match="evidence changed"):
            apply_printing_correction(session, card, reviewed, current)
        session.rollback()
    with Session(db) as session:
        assert session.query(InventoryCard).one().set_code == "SUM"
        assert session.query(RemoteProductBinding).one().product_id == "summer-lp"


def test_ui_printing_picker_uses_scryfall_and_filters_current_finish(db, monkeypatch):
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: [
        {"id": NEW_SCRYFALL, "name": name, "set": "3ed", "set_name": "Revised",
         "collector_number": "261", "lang": "en", "finishes": ["nonfoil"],
         "released_at": "1994-04-01"},
        {"id": "foil-only", "name": name, "set": "x", "set_name": "Foil Set",
         "collector_number": "1", "lang": "en", "finishes": ["foil"],
         "released_at": "2000-01-01"},
    ])
    html = main.inventory_printing_correction_options(1)
    assert "Revised (3ED) #261" in html
    assert NEW_SCRYFALL in html
    assert "foil-only" not in html
