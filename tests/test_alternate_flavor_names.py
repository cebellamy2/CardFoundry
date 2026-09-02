"""Alternate/flavor names alongside canonical names: display format and
/inventory search matching. Covers _card_display_name directly, plus the
/inventory route's search-by-flavor-name extension and display sweep
(edit page, search results table)."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from main import _card_display_name
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'alt_flavor_names.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, batch_code="A1", **overrides):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code, is_archived=False)
        session.add(batch)
        session.flush()
    values = {"batch_id": batch.id, "name": "Roaming Throne", "status": "available"}
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


# --- _card_display_name (pure function) -------------------------------------

def test_display_name_with_flavor_name_is_alt_paren_canonical():
    assert _card_display_name("Roaming Throne", "Doom Variant") == "Doom Variant (Roaming Throne)"


def test_display_name_without_flavor_name_is_unchanged():
    assert _card_display_name("Lightning Bolt", None) == "Lightning Bolt"


def test_display_name_with_empty_string_flavor_name_is_unchanged():
    assert _card_display_name("Lightning Bolt", "") == "Lightning Bolt"


# --- /inventory search matches flavor_name too -------------------------------

def test_search_by_flavor_name_finds_the_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
    response = TestClient(main.app).get("/inventory", params={"q": "Doom Variant"})
    assert response.status_code == 200
    assert "Doom Variant (Roaming Throne)" in response.text


def test_search_by_flavor_name_is_substring_and_case_insensitive(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
    response = TestClient(main.app).get("/inventory", params={"q": "doom"})
    assert response.status_code == 200
    assert "Doom Variant (Roaming Throne)" in response.text


def test_search_by_canonical_name_still_finds_a_flavor_named_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
    response = TestClient(main.app).get("/inventory", params={"q": "Roaming Throne"})
    assert response.status_code == 200
    assert "Doom Variant (Roaming Throne)" in response.text


def test_search_does_not_cross_match_unrelated_flavor_names(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
        add_card(session, name="Brainstorm")
    response = TestClient(main.app).get("/inventory", params={"q": "Brainstorm"})
    assert response.status_code == 200
    assert "Brainstorm" in response.text
    assert "Doom Variant" not in response.text


# --- display sweep: search results table + edit page -------------------------

def test_search_results_table_shows_alt_name(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
    response = TestClient(main.app).get("/inventory", params={"q": "Roaming"})
    assert 'class="card-name"' in response.text
    assert "Doom Variant (Roaming Throne)" in response.text


def test_search_results_card_without_flavor_name_renders_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Brainstorm")
    response = TestClient(main.app).get("/inventory", params={"q": "Brainstorm"})
    assert "Brainstorm" in response.text
    assert "(Brainstorm)" not in response.text


def test_edit_page_heading_shows_alt_name(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert "Doom Variant (Roaming Throne)" in response.text
    # The editable "Card Name" input value must stay canonical-only -- it
    # writes back to card.name on save, so injecting alt-name text there
    # would corrupt the stored value.
    assert 'value="Doom Variant (Roaming Throne)"' not in response.text
    assert 'value="Roaming Throne"' in response.text


def test_edit_page_back_to_search_link_stays_canonical_name(tmp_path, monkeypatch):
    """/inventory?q= filters by substring against the stored name/flavor
    columns -- the canonical name alone already finds this card, so the
    link is left unchanged rather than risking a query string that
    over-matches or under-matches."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Roaming Throne", flavor_name="Doom Variant")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert "/inventory?q=Roaming Throne" in response.text


def test_sold_status_card_with_flavor_name_still_displays_alt_name(tmp_path, monkeypatch):
    """At least one non-available-status row must still get the display
    treatment -- not just the common 'available' case."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(
            session, name="Roaming Throne", flavor_name="Doom Variant",
            status="sold", sold_price=25.0,
        )
    response = TestClient(main.app).get("/inventory", params={"q": "Roaming", "status": "sold"})
    assert response.status_code == 200
    assert "Doom Variant (Roaming Throne)" in response.text
