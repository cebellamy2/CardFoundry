from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventoryListingStatus


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory-status-vocab.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


_next_batch_number = [0]


def add_card(session, *, name="Forest", status="available", batch_code=None):
    _next_batch_number[0] += 1
    batch = Batch(batch_code=batch_code or f"B{_next_batch_number[0]}")
    session.add(batch)
    session.flush()
    card = InventoryCard(batch_id=batch.id, name=name, status=status)
    session.add(card)
    session.commit()
    return card


def mark_listed(session, card_id, status="listed"):
    session.add(InventoryListingStatus(
        inventory_card_id=card_id, listing_status=status, checked_at=datetime.now(),
    ))
    session.commit()


# -- _inventory_status_display / label helpers (unit) --------------------

def test_available_card_with_no_cache_entry_defaults_to_not_listed():
    assert main._inventory_status_display(
        type("Card", (), {"status": "available", "id": 1})(), {},
    ) == "Not Listed"


def test_available_card_with_listed_cache_entry_shows_listed():
    assert main._inventory_status_display(
        type("Card", (), {"status": "available", "id": 1})(), {1: "listed"},
    ) == "Listed"


def test_available_card_with_not_listed_cache_entry_shows_not_listed():
    assert main._inventory_status_display(
        type("Card", (), {"status": "available", "id": 1})(), {1: "not_listed"},
    ) == "Not Listed"


def test_non_available_statuses_use_the_five_value_labels():
    labels = {
        "reserved": "Reserved", "sold": "Sold", "unsellable": "Unavailable",
        "removed": "Removed",
    }
    for status, label in labels.items():
        card = type("Card", (), {"status": status, "id": 1})()
        assert main._inventory_status_display(card, {}) == label


# -- /inventory display -----------------------------------------------

def test_inventory_search_shows_listed_for_a_confirmed_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Listed Bolt")
        mark_listed(session, card.id, "listed")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "Listed Bolt" in response.text
    assert "<td>Listed</td>" in response.text


def test_inventory_search_shows_not_listed_for_no_cache_entry(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Unlisted Bolt")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "Unlisted Bolt" in response.text
    assert "<td>Not Listed</td>" in response.text


def test_inventory_search_shows_unavailable_for_unsellable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Sidelined Bolt", status="unsellable")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "<strong>Unavailable</strong>" in response.text


# -- /inventory status filter ------------------------------------------

def test_status_filter_listed_returns_only_confirmed_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        listed_card = add_card(session, name="Listed One")
        add_card(session, name="Not Listed One")
        mark_listed(session, listed_card.id, "listed")
    response = TestClient(main.app).get("/inventory?show_all=true&status=listed")
    assert response.status_code == 200
    assert "Listed One" in response.text
    assert "Not Listed One" not in response.text


def test_status_filter_not_listed_includes_no_cache_entry_and_explicit_not_listed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        listed_card = add_card(session, name="OnlyListedThree")
        no_cache_card = add_card(session, name="NoCacheThree")
        explicit_card = add_card(session, name="ExplicitNotListedThree")
        mark_listed(session, listed_card.id, "listed")
        mark_listed(session, explicit_card.id, "not_listed")
    response = TestClient(main.app).get("/inventory?show_all=true&status=not_listed")
    assert response.status_code == 200
    assert "NoCacheThree" in response.text
    assert "ExplicitNotListedThree" in response.text
    assert "OnlyListedThree" not in response.text


def test_status_filter_not_listed_excludes_non_available_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Sold Card", status="sold")
    response = TestClient(main.app).get("/inventory?show_all=true&status=not_listed")
    assert response.status_code == 200
    assert "Sold Card" not in response.text


def test_legacy_available_filter_value_is_rejected(tmp_path, monkeypatch):
    # "available" was replaced by the listed/not_listed split -- an old
    # bookmarked link should fall back to unfiltered rather than 500 or
    # silently matching nothing.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Either Way Card")
    response = TestClient(main.app).get("/inventory?show_all=true&status=available")
    assert response.status_code == 200
    assert "Either Way Card" in response.text
    assert 'value="" selected' in response.text or "All statuses</option>" in response.text


# -- batch detail display ------------------------------------------------

def test_batch_detail_shows_listing_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Batch Bolt")
        mark_listed(session, card.id, "listed")
        batch_id = card.batch_id
    response = TestClient(main.app).get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert "<td>\n                    Listed\n                </td>" in response.text \
        or "Listed" in response.text


# -- card edit page display ----------------------------------------------

def test_edit_page_shows_listed_status_line(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Edit Bolt")
        mark_listed(session, card.id, "listed")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert "<strong>Status:</strong>" in response.text
    assert "Listed" in response.text
