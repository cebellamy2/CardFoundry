import contextlib
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
        "color_identity": ["U"],
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


def revised_seller_listing():
    return {
        "id": "revised-inventory", "product_id": "revised-lp",
        "product_type": "mtg_single", "product": {"single": {
            "name": "Library of Leng", "set": "3ED", "number": "261",
            "scryfall_id": NEW_SCRYFALL, "mtgjson_id": "mtg-revised-261",
            "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        }},
    }


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


def preview(session, card, seller_inventory=None):
    return build_printing_correction_preview(
        session, card, NEW_SCRYFALL,
        [revised_seller_listing()] if seller_inventory is None else seller_inventory,
        catalog_lookup, scryfall_lookup,
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


def test_available_printing_correction_requires_canonical_mtgjson(db):
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        with pytest.raises(PrintingCorrectionError, match="MTGJSON|canonical"):
            preview(session, card, seller_inventory=[])
        assert card.status == "available"
        assert card.mtgjson_id is None


def test_confirm_atomically_applies_canonical_seller_correction_and_audits(db):
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        reviewed = preview(session, card)
        current = preview(session, card)
        apply_printing_correction(session, card, reviewed, current)
        session.commit()
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        assert (card.set_code, card.collector_number, card.scryfall_id) == (
            "3ED", "261", NEW_SCRYFALL,
        )
        assert card.mtgjson_id == "mtg-revised-261"
        assert card.color_identity == "U"
        assert session.query(RemoteProductBinding).count() == 0
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


def test_confirm_route_shows_card_name_not_bare_id(db, monkeypatch):
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "inventory_sync_lease", lambda: contextlib.nullcontext())
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda **kw: [revised_seller_listing()])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "fetch_scryfall_cards", scryfall_lookup)
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        reviewed = preview(session, card)
        card_id = card.id
    body = main.confirm_inventory_printing_correction(
        card_id, replacement_scryfall_id=NEW_SCRYFALL, reviewed_json=json.dumps(reviewed),
    )
    assert f"Library of Leng (#{card_id})" in body
    assert f">{card_id} was updated locally" not in body


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
