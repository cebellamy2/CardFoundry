import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, Consignor, ConsignorPayout, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignment-routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
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


def make_owed_card(db, batch_id, **overrides):
    values = {
        "status": "sold", "sold_price": 10.0,
        "consignment_amount_owed": 8.0, "consignment_payout_status": "owed",
    }
    values.update(overrides)
    return make_card(db, batch_id, **values)


def extract_hidden(html, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    return match.group(1) if match else None


# --- new_consignor_payout_form ---

def test_pay_form_shows_owed_cards_and_prefilled_method(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe", payout_method="Cash App: @jane")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    make_owed_card(db, batch.id, name="Lightning Bolt")
    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/pay")
    assert response.status_code == 200
    assert "Lightning Bolt" in response.text
    assert 'value="Cash App: @jane"' in response.text
    assert "Total if every checked card is included: $8.00" in response.text


def test_pay_form_shows_nothing_owed_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/pay")
    assert response.status_code == 200
    assert "nothing currently owed" in response.text


def test_pay_form_missing_consignor_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/consignors/999/pay").status_code == 404


# --- preview_consignor_payout ---

def test_pay_preview_computes_total_for_selected_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card1 = make_owed_card(db, batch.id, name="Card A", consignment_amount_owed=8.0)
    card2 = make_owed_card(db, batch.id, name="Card B", consignment_amount_owed=16.0)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/pay/preview",
        data={
            "card_ids": [str(card1.id), str(card2.id)],
            "method": "Cash App", "note": "test", "paid_at": "2026-08-19",
        },
    )
    assert response.status_code == 200
    assert "Total: $24.00" in response.text
    assert "Confirm Payout" in response.text


def test_pay_preview_rejects_empty_selection(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    response = client.post(f"/consignors/{consignor.id}/pay/preview", data={"card_ids": []})
    assert response.status_code == 400


def test_pay_preview_rejects_card_not_owed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id, consignment_payout_status="paid")
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/pay/preview",
        data={"card_ids": [str(card.id)]},
    )
    assert response.status_code == 409


def test_pay_preview_rejects_card_belonging_to_other_consignor(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor_a = make_consignor(db, name="A")
    consignor_b = make_consignor(db, name="B")
    batch_b = make_batch(db, "CONSIGN-B", is_consignment=True, consignor_id=consignor_b.id)
    card = make_owed_card(db, batch_b.id)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor_a.id}/pay/preview",
        data={"card_ids": [str(card.id)]},
    )
    assert response.status_code == 400


# --- confirm_consignor_payout ---

def test_pay_confirm_creates_payout_and_marks_cards_paid(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor.id}/pay/confirm",
        data={
            "card_ids": [str(card.id)], "method": "Cash App",
            "note": "first payout", "paid_at": "2026-08-19",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/consignors/{consignor.id}/payouts"

    with Session(db) as session:
        payout = session.query(ConsignorPayout).one()
        assert payout.amount == 8.0
        assert payout.method == "Cash App"
        refreshed = session.get(InventoryCard, card.id)
        assert refreshed.consignment_payout_status == "paid"
        assert refreshed.consignment_payout_id == payout.id


def test_pay_confirm_covers_only_selected_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card1 = make_owed_card(db, batch.id, name="Card A")
    card2 = make_owed_card(db, batch.id, name="Card B")
    client = TestClient(main.app)
    client.post(
        f"/consignors/{consignor.id}/pay/confirm",
        data={"card_ids": [str(card1.id)], "paid_at": "2026-08-19"},
        follow_redirects=False,
    )
    with Session(db) as session:
        assert session.get(InventoryCard, card1.id).consignment_payout_status == "paid"
        assert session.get(InventoryCard, card2.id).consignment_payout_status == "owed"


def test_pay_confirm_refuses_double_pay(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    client = TestClient(main.app)
    client.post(
        f"/consignors/{consignor.id}/pay/confirm",
        data={"card_ids": [str(card.id)], "paid_at": "2026-08-19"},
        follow_redirects=False,
    )
    response = client.post(
        f"/consignors/{consignor.id}/pay/confirm",
        data={"card_ids": [str(card.id)], "paid_at": "2026-08-19"},
    )
    assert "Payout Refused" in response.text
    with Session(db) as session:
        assert session.query(ConsignorPayout).count() == 1


# --- consignor_payout_history_page ---

def test_payout_history_page_shows_recorded_payout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    client = TestClient(main.app)
    client.post(
        f"/consignors/{consignor.id}/pay/confirm",
        data={"card_ids": [str(card.id)], "method": "Cash App", "paid_at": "2026-08-19"},
        follow_redirects=False,
    )
    response = client.get(f"/consignors/{consignor.id}/payouts")
    assert response.status_code == 200
    assert "$8.00" in response.text
    assert "Cash App" in response.text
    assert "1 card(s)" in response.text


def test_payout_history_page_shows_empty_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/payouts")
    assert response.status_code == 200
    assert "No payouts recorded yet." in response.text


# --- payout correction ---

def make_payout(db, consignor_id, card_id, **overrides):
    with Session(db) as session:
        values = {"consignor_id": consignor_id, "amount": 8.0, "method": "Cash App", "note": ""}
        values.update(overrides)
        payout = ConsignorPayout(**values)
        session.add(payout)
        session.flush()
        card = session.get(InventoryCard, card_id)
        card.consignment_payout_status = "paid"
        card.consignment_payout_id = payout.id
        session.commit()
        session.refresh(payout)
        return payout


def test_edit_payout_form_shows_current_values(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id, consignment_payout_status="owed")
    payout = make_payout(db, consignor.id, card.id, amount=8.0, method="Cash App")
    client = TestClient(main.app)
    response = client.get(f"/consignors/payouts/{payout.id}/edit")
    assert response.status_code == 200
    assert 'value="8.0"' in response.text
    assert 'value="Cash App"' in response.text


def test_edit_payout_form_missing_payout_404s(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/consignors/payouts/999/edit").status_code == 404


def test_payout_correction_full_flow_updates_amount(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    payout = make_payout(db, consignor.id, card.id, amount=8.0)
    client = TestClient(main.app)

    preview = client.post(
        f"/consignors/payouts/{payout.id}/correction/preview",
        data={
            "new_amount": "6.00", "new_method": "Venmo", "new_note": "refund",
            "new_paid_at": "2026-08-19", "correction_reason": "Partial refund",
        },
    )
    assert preview.status_code == 200
    assert "$6.00" in preview.text
    state_hash = extract_hidden(preview.text, "expected_state_hash")
    assert state_hash

    confirm = client.post(
        f"/consignors/payouts/{payout.id}/correction/confirm",
        data={
            "expected_state_hash": state_hash, "new_amount": "6.00",
            "new_method": "Venmo", "new_note": "refund",
            "new_paid_at": "2026-08-19", "correction_reason": "Partial refund",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    assert confirm.headers["location"] == f"/consignors/{consignor.id}/payouts"

    with Session(db) as session:
        refreshed = session.get(ConsignorPayout, payout.id)
        assert refreshed.amount == 6.0
        assert refreshed.method == "Venmo"


def test_payout_correction_confirm_refuses_stale_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    payout = make_payout(db, consignor.id, card.id, amount=8.0)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/payouts/{payout.id}/correction/confirm",
        data={
            "expected_state_hash": "stale", "new_amount": "6.00",
            "new_method": "Venmo", "new_note": "", "new_paid_at": "2026-08-19",
            "correction_reason": "reason",
        },
    )
    assert "Payout Correction Refused" in response.text
    with Session(db) as session:
        assert session.get(ConsignorPayout, payout.id).amount == 8.0


def test_payout_correction_preview_requires_reason(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor(db, name="Jane Doe")
    batch = make_batch(db, "CONSIGN-1", is_consignment=True, consignor_id=consignor.id)
    card = make_owed_card(db, batch.id)
    payout = make_payout(db, consignor.id, card.id, amount=8.0)
    client = TestClient(main.app)
    response = client.post(
        f"/consignors/payouts/{payout.id}/correction/preview",
        data={
            "new_amount": "6.00", "new_method": "Venmo", "new_note": "",
            "new_paid_at": "2026-08-19", "correction_reason": "   ",
        },
    )
    assert response.status_code == 400
