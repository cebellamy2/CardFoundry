from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, ImportRecord, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'import-undo-routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_import(db, *, status="active", card_count=2):
    with Session(db) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch); session.flush()
        record = ImportRecord(
            batch_id=batch.id, filename="a1.csv", file_hash="hash-a1",
            card_count=card_count, status=status,
        )
        session.add(record); session.flush()
        for i in range(card_count):
            session.add(InventoryCard(
                batch_id=batch.id, import_id=record.id, name=f"Card {i}",
                set_code="SET", collector_number=str(i), scryfall_id=f"sf-{i}",
                mtgjson_id=f"mtg-{i}", language_id="EN", condition_id="LP",
                finish_id="NF", condition="LP", finish="normal", status="available",
            ))
        session.commit()
        return record.id


def test_import_detail_shows_undo_form_when_active_with_available_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db)
    response = TestClient(main.app).get(f"/imports/{import_id}")
    assert response.status_code == 200
    assert f'action="/imports/{import_id}/undo"' in response.text
    assert "2 of 2 card(s)" in response.text


def test_import_detail_no_undo_form_when_already_reversed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db, status="reversed")
    response = TestClient(main.app).get(f"/imports/{import_id}")
    assert response.status_code == 200
    assert f'action="/imports/{import_id}/undo"' not in response.text
    assert "already been undone" in response.text


def test_import_detail_404_for_missing_import(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/imports/999")
    assert response.status_code == 404


def test_undo_import_route_success_removes_cards_and_marks_reversed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db)
    client = TestClient(main.app)
    response = client.post(f"/imports/{import_id}/undo", data={"note": "Wrong batch entirely"})
    assert response.status_code == 200
    assert "Import Undone" in response.text

    with Session(db) as session:
        record = session.get(ImportRecord, import_id)
        assert record.status == "reversed"
        cards = session.query(InventoryCard).filter(InventoryCard.import_id == import_id).all()
        assert all(card.status == "removed" for card in cards)


def test_undo_import_route_reports_skipped_cards_and_stays_active(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db, card_count=2)
    with Session(db) as session:
        card = session.query(InventoryCard).filter(InventoryCard.import_id == import_id).first()
        card.status = "sold"
        session.commit()

    client = TestClient(main.app)
    response = client.post(f"/imports/{import_id}/undo", data={"note": "Undo reason"})
    assert response.status_code == 200
    assert "Not Removed (1)" in response.text

    with Session(db) as session:
        record = session.get(ImportRecord, import_id)
        # Still "active" -- real inventory (the sold card) is still tied
        # to this import, so it hasn't been fully undone.
        assert record.status == "active"


def test_undo_import_route_requires_a_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db)
    client = TestClient(main.app)
    response = client.post(f"/imports/{import_id}/undo", data={"note": "   "})
    assert response.status_code == 400
    assert "Undo Refused" in response.text
    with Session(db) as session:
        assert session.get(ImportRecord, import_id).status == "active"


def test_undo_import_route_refused_when_already_reversed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db, status="reversed")
    client = TestClient(main.app)
    response = client.post(f"/imports/{import_id}/undo", data={"note": "note"})
    assert response.status_code == 409
    assert "Already Undone" in response.text


def test_undo_import_route_404_for_missing_import(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/imports/999/undo", data={"note": "note"})
    assert response.status_code == 404


def test_import_history_page_links_to_detail(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    import_id = make_import(db)
    response = TestClient(main.app).get("/imports")
    assert response.status_code == 200
    assert f'href="/imports/{import_id}"' in response.text
