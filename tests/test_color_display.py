from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'color.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, name="Forest", color=None, status="available"):
    batch = Batch(batch_code=f"B-{name}-{color}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="SET", collector_number="1",
        scryfall_id="sf-1", condition="LP", condition_id="LP", finish="normal",
        finish_id="NF", language_id="EN", status=status, color=color,
    )
    session.add(card)
    session.commit()
    return card


def test_badge_renders_a_chip_per_letter_in_wubrg_order():
    assert main._color_badge("WU") == (
        '<span class="color-pip color-pip-w" title="White">W</span>'
        '<span class="color-pip color-pip-u" title="Blue">U</span>'
    )


def test_badge_shows_colorless_chip_for_empty_string():
    assert main._color_badge("") == (
        '<span class="color-pip color-pip-c" title="Colorless">C</span>'
    )


def test_badge_shows_nothing_when_not_yet_resolved():
    assert main._color_badge(None) == ""


def test_text_variant_for_escaped_detail_tables():
    assert main._color_text("WU") == "(WU)"
    assert main._color_text("") == "(Colorless)"
    assert main._color_text(None) == ""


def test_inventory_search_shows_color_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, color="G")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert 'color-pip-g" title="Green">G</span>' in response.text


def test_edit_page_shows_color_badge_in_header(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, color="UB")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert "Edit Physical Card: Forest" in response.text
    assert 'color-pip-u" title="Blue">U</span>' in response.text
    assert 'color-pip-b" title="Black">B</span>' in response.text


def test_unresolved_color_shows_no_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, color=None)
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "<span class=\"color-pip" not in response.text


def test_land_backfilled_as_colorless_shows_no_badge(tmp_path, monkeypatch):
    """Forest's mana cost has no colored symbols -- color is stored as "",
    same as any other colorless card. Deliberate: unlike MTG's broader
    "color identity" concept, lands are not treated as their produced
    color here."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Forest", color="")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert 'color-pip-c" title="Colorless">C</span>' in response.text
    assert 'color-pip-g" title="Green">G</span>' not in response.text
