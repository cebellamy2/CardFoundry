"""CF-SCAN-025: /inventory/{card_id}/set-price -- the Needs Price
category's own set-price action. Deliberately NOT the v1.57.0 manual-
price-override flow reused verbatim (that flow writes a
ManualPriceOverride row and explicitly never touches InventoryCard.
price_usd) -- this targets InventoryCard.price_usd/current_price
directly and clears price_pending_since in the same write.
"""

import re
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'set-price.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def add_card(session, **overrides):
    batch = Batch(batch_code=overrides.pop("batch_code", "B1"))
    session.add(batch)
    session.flush()
    values = {
        "batch_id": batch.id, "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "status": "available", "price_pending_since": datetime(2026, 9, 6, 12, 0),
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def test_set_price_review_page_shows_card_and_hidden_hash(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    response = TestClient(main.app).get(f"/inventory/{card_id}/set-price")
    assert response.status_code == 200
    assert "Alpha" in response.text
    assert 'name="expected_state_hash" value="' in response.text


def test_set_price_review_page_refuses_a_card_not_awaiting_price(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, price_pending_since=None, price_usd=5.0)
        session.commit()
        card_id = card.id

    response = TestClient(main.app).get(f"/inventory/{card_id}/set-price")
    assert response.status_code == 409


def _submit(client, card_id, page_text, **field_overrides):
    match = re.search(r'name="expected_state_hash" value="([^"]+)"', page_text)
    data = {
        "price_dollars": "3.50", "note": "Priced from a bulk box lot.",
        "confirmation": main.SET_SCANNED_PRICE_CONFIRMATION,
        "expected_state_hash": match.group(1) if match else "",
    }
    data.update(field_overrides)
    return client.post(f"/inventory/{card_id}/set-price", data=data)


def test_set_price_confirm_sets_price_and_clears_hold(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    page = client.get(f"/inventory/{card_id}/set-price")
    response = _submit(client, card_id, page.text)
    assert response.status_code == 200, response.text
    assert "Price set" in response.text

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.price_usd == 3.50
        assert card.current_price == 3.50
        assert card.price_pending_since is None


def test_set_price_confirm_rejects_wrong_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    page = client.get(f"/inventory/{card_id}/set-price")
    response = _submit(client, card_id, page.text, confirmation="nope")
    assert response.status_code == 400

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.price_usd is None
        assert card.price_pending_since is not None


def test_set_price_confirm_requires_a_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    page = client.get(f"/inventory/{card_id}/set-price")
    response = _submit(client, card_id, page.text, note="")
    assert response.status_code == 400

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.price_pending_since is not None


def test_set_price_confirm_rejects_an_invalid_dollar_amount(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    page = client.get(f"/inventory/{card_id}/set-price")
    response = _submit(client, card_id, page.text, price_dollars="not-a-price")
    assert response.status_code == 400

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.price_pending_since is not None


def test_set_price_confirm_rejects_a_stale_state_hash(tmp_path, monkeypatch):
    """The card was already priced (or its hold otherwise changed) in
    another tab between page load and this submit -- a stale hidden
    hash must be refused, not silently overwrite the newer state."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        session.commit()
        card_id = card.id

    client = TestClient(main.app)
    page = client.get(f"/inventory/{card_id}/set-price")

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        card.price_usd = 9.99
        card.current_price = 9.99
        card.price_pending_since = None
        session.commit()

    response = _submit(client, card_id, page.text)
    assert response.status_code == 409

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.price_usd == 9.99


def test_set_price_confirm_unknown_card_returns_409(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = _submit(client, 999999, "", price_dollars="3.50")
    assert response.status_code == 409
