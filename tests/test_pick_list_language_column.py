from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import InventoryCard
from tests.test_order_detail_item13_redesign import make_order_with_allocation, setup_db as setup_order_db
from tests.test_pick_list_finish_highlight import make_wave_with_card, setup_db as setup_wave_db


# Master Pick List (/pick-waves/{wave_id})

def test_master_pick_list_shows_language_header(tmp_path, monkeypatch):
    db = setup_wave_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="normal", finish_id="NF")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "<th>Language</th>" in response.text


def test_master_pick_list_shows_english_language_unconditionally(tmp_path, monkeypatch):
    db = setup_wave_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="normal", finish_id="NF")
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    # make_wave_with_card's card is language_id="EN" -- must still show,
    # not be suppressed/blank for English.
    tr_idx = response.text.rindex("<tr", 0, response.text.index("Alpha"))
    row = response.text[tr_idx:tr_idx + 500]
    assert "<td>EN</td>" in row


def test_master_pick_list_shows_foreign_language_code(tmp_path, monkeypatch):
    db = setup_wave_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="normal", finish_id="NF")
        card = session.query(InventoryCard).one()
        card.language_id = "JA"
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "<td>JA</td>" in response.text


def test_master_pick_list_blank_language_renders_empty_not_none(tmp_path, monkeypatch):
    db = setup_wave_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="normal", finish_id="NF")
        card = session.query(InventoryCard).one()
        card.language_id = None
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "None" not in response.text
    assert "<td></td>" in response.text


# Order Detail's own picklist (/orders/{order_id})

def test_order_detail_picklist_shows_language_header(tmp_path, monkeypatch):
    db = setup_order_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "<th>Language</th>" in response.text


def test_order_detail_picklist_shows_english_language_unconditionally(tmp_path, monkeypatch):
    db = setup_order_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "EN" in response.text


def test_order_detail_picklist_shows_foreign_language_code(tmp_path, monkeypatch):
    db = setup_order_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="picked", allocation_status="picked",
        )
        card.language_id = "DE"
        session.commit()
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "DE" in response.text
