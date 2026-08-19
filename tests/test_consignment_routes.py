from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignment-routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_consignor(db, name="Jane", **overrides):
    with Session(db) as session:
        consignor = Consignor(name=name, **overrides)
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
        return consignor


def make_batch(db, code, *, is_consignment=False, consignor_id=None):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_consignment=is_consignment, consignor_id=consignor_id)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def make_card(db, batch_id, **overrides):
    with Session(db) as session:
        values = {"batch_id": batch_id, "name": "Alpha", "status": "available"}
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.commit()
        session.refresh(card)
        return card


# --- consignors_page ---

def test_consignors_page_shows_empty_state(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/consignors")
    assert response.status_code == 200
    assert "No consignors yet." in response.text


def test_consignors_page_lists_name_payout_method_and_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    client = TestClient(main.app)
    response = client.get("/consignors")
    assert response.status_code == 200
    assert "Jane Doe" in response.text
    assert "Cash App: @jane" in response.text
    assert "Active" in response.text


# --- create_consignor ---

def test_create_consignor_happy_path_redirects_to_list(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/consignors",
        data={"name": "Jane Doe", "contact_info": "text: 555-1234", "payout_method": "Venmo: @jane"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/consignors"

    with Session(db) as session:
        consignor = session.query(Consignor).one()
        assert consignor.name == "Jane Doe"
        assert consignor.contact_info == "text: 555-1234"
        assert consignor.payout_method == "Venmo: @jane"
        assert consignor.is_active is True


def test_create_consignor_blank_name_rejected(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/consignors", data={"name": "   "})
    assert response.status_code == 400
    with Session(db) as session:
        assert session.query(Consignor).count() == 0


# --- edit_consignor_form / update_consignor ---

def test_edit_consignor_form_missing_id_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/consignors/999/edit").status_code == 404


def test_edit_consignor_form_shows_current_values(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/edit")
    assert response.status_code == 200
    assert "Jane Doe" in response.text
    assert "Cash App: @jane" in response.text


def test_update_consignor_persists_changes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/edit",
        data={"name": "Jane D.", "contact_info": "", "payout_method": "Venmo: @jd"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(Consignor, consignor.id)
        assert refreshed.name == "Jane D."
        assert refreshed.payout_method == "Venmo: @jd"
        assert refreshed.is_active is False


def test_update_consignor_keeps_active_when_checkbox_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    client.post(
        f"/consignors/{consignor.id}/edit",
        data={"name": "Jane Doe", "is_active": "true"},
        follow_redirects=False,
    )
    with Session(db) as session:
        assert session.get(Consignor, consignor.id).is_active is True


def test_update_consignor_missing_id_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/consignors/999/edit", data={"name": "Ghost"})
    assert response.status_code == 404


def test_update_consignor_blank_name_rejected(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    response = client.post(f"/consignors/{consignor.id}/edit", data={"name": "  "})
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(Consignor, consignor.id).name == "Jane Doe"


# --- consignors_owed_report ---

def test_owed_report_shows_empty_state(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/consignors/owed")
    assert response.status_code == 200
    assert "Nothing currently owed" in response.text


def test_owed_report_shows_consignor_total_and_card_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    make_card(
        db, batch.id, name="Lightning Bolt", status="sold",
        consignment_value=8.00, sold_price=10.00,
        consignment_amount_owed=8.00, consignment_payout_status="owed",
    )
    client = TestClient(main.app)
    response = client.get("/consignors/owed")
    assert response.status_code == 200
    assert "Jane Doe" in response.text
    assert "Lightning Bolt" in response.text
    assert "$8.00" in response.text
    assert "Cash App: @jane" in response.text


# --- new_batch_form consignor picker ---

def test_new_batch_form_lists_only_active_consignors(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_consignor(db, name="Active Jane", is_active=True)
    make_consignor(db, name="Inactive Bob", is_active=False)
    client = TestClient(main.app)
    response = client.get("/batches/new")
    assert response.status_code == 200
    assert "Active Jane" in response.text
    assert "Inactive Bob" not in response.text


# --- create_batch consignment fields ---

def test_create_consignment_batch_requires_consignor(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/batches", data={"batch_code": "CONSIGN-1", "is_consignment": "true"})
    assert response.status_code == 400


def test_create_consignment_batch_rejects_unknown_consignor(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/batches",
        data={"batch_code": "CONSIGN-1", "is_consignment": "true", "consignor_id": "999"},
    )
    assert response.status_code == 404


def test_create_consignment_batch_happy_path(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    response = client.post(
        "/batches",
        data={"batch_code": "CONSIGN-1", "is_consignment": "true", "consignor_id": str(consignor.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        batch = session.query(Batch).filter(Batch.batch_code == "CONSIGN-1").one()
        assert batch.is_consignment is True
        assert batch.consignor_id == consignor.id


def test_create_batch_without_consignment_leaves_fields_unset(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/batches", data={"batch_code": "NORMAL-1"}, follow_redirects=False)
    with Session(db) as session:
        batch = session.query(Batch).filter(Batch.batch_code == "NORMAL-1").one()
        assert batch.is_consignment is False
        assert batch.consignor_id is None


def test_reusing_existing_batch_code_does_not_mutate_consignment_flags(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    make_batch(db, "EXIST-1", is_consignment=False, consignor_id=None)
    client = TestClient(main.app)
    client.post(
        "/batches",
        data={"batch_code": "EXIST-1", "is_consignment": "true", "consignor_id": str(consignor.id)},
        follow_redirects=False,
    )
    with Session(db) as session:
        batch = session.query(Batch).filter(Batch.batch_code == "EXIST-1").one()
        assert batch.is_consignment is False
        assert batch.consignor_id is None


# --- edit_inventory_card consignment block ---

def test_edit_card_page_shows_consignment_block_for_consignment_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_card(db, batch.id, name="Lightning Bolt", consignment_value=8.00)
    client = TestClient(main.app)
    response = client.get(f"/inventory/{card.id}/edit")
    assert response.status_code == 200
    assert "Jane Doe" in response.text
    assert 'name="consignment_value"' in response.text
    assert 'value="8.0"' in response.text


def test_edit_card_page_hides_consignment_block_for_non_consignment_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "NORMAL-1", is_consignment=False)
    card = make_card(db, batch.id, name="Lightning Bolt")
    client = TestClient(main.app)
    response = client.get(f"/inventory/{card.id}/edit")
    assert response.status_code == 200
    assert 'name="consignment_value"' not in response.text


def test_edit_card_page_shows_owed_line_when_set(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_card(
        db, batch.id, name="Lightning Bolt", status="sold",
        consignment_amount_owed=8.00, consignment_payout_status="owed",
    )
    client = TestClient(main.app)
    response = client.get(f"/inventory/{card.id}/edit")
    assert response.status_code == 200
    assert "Owed: $8.00" in response.text
    assert "(owed)" in response.text


# --- save_inventory_card consignment fields ---

def test_save_card_persists_consignment_value_and_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_card(db, batch.id, name="Lightning Bolt")
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data={
            "name": "Lightning Bolt",
            "batch_id": str(batch.id),
            "consignment_value": "12.50",
            "consignment_note": "Mint condition, taken 2026-08-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(InventoryCard, card.id)
        assert refreshed.consignment_value == 12.50
        assert refreshed.consignment_note == "Mint condition, taken 2026-08-01"


def test_save_card_rejects_non_numeric_consignment_value(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "CONSIGN-1", is_consignment=True)
    card = make_card(db, batch.id, name="Lightning Bolt")
    client = TestClient(main.app)
    response = client.post(
        f"/inventory/{card.id}/edit",
        data={
            "name": "Lightning Bolt",
            "batch_id": str(batch.id),
            "consignment_value": "not-a-number",
        },
    )
    assert response.status_code == 400
