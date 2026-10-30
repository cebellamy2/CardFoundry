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


def add_card(session, *, name="Forest", color=None, status="available",
             type_line=None):
    batch = Batch(batch_code=f"B-{name}-{color}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="SET", collector_number="1",
        scryfall_id="sf-1", condition="LP", condition_id="LP", finish="normal",
        finish_id="NF", language_id="EN", status=status, color=color,
        type_line=type_line,
    )
    session.add(card)
    session.commit()
    return card


def test_badge_renders_a_chip_per_letter_in_wubrg_order():
    assert main._color_badge("WU") == (
        '<span class="color-pip color-pip-w" title="White">W</span>'
        '<span class="color-pip color-pip-u" title="Blue">U</span>'
    )


def test_badge_shows_colorless_chip_for_a_colorless_NON_land():
    """v2.76.1: the badge needs the type line as well as the colour.

    This used to assert that `color == ""` alone meant C. It cannot: a
    land stores '' too, which is how every land came to show C. C now
    means "resolved colourless AND we know it is not a land", so the
    type line is part of the question.
    """
    assert main._color_badge("", "Artifact") == (
        '<span class="color-pip color-pip-c" title="Colorless">C</span>'
    )


def test_badge_shows_nothing_for_an_empty_colour_with_no_type_line():
    """"Colourless" and "not looked up yet" are different states, and only
    the type line tells them apart -- so an unknown type gets no badge
    rather than a confident, wrong C. Same rule the packing slip prints
    by; see card_color_marker."""
    assert main._color_badge("", None) == ""


def test_badge_shows_nothing_when_not_yet_resolved():
    assert main._color_badge(None) == ""


def test_inventory_search_shows_color_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, color="G")
    response = TestClient(main.app).get("/inventory?show_all=true&status_submitted=1")
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
    response = TestClient(main.app).get("/inventory?show_all=true&status_submitted=1")
    assert response.status_code == 200
    assert "<span class=\"color-pip" not in response.text


def test_a_land_shows_L_not_the_colorless_chip(tmp_path, monkeypatch):
    """v2.76.1, operator report 2026-10-28.

    Forest's mana cost has no coloured symbols, so `color` is stored as
    "" exactly as for any colourless card -- which is why this page used
    to show it as C. It is a LAND, and a land is (L) whatever its colour,
    the same rule the packing slip has printed since v2.68.0. Lands are
    still not treated as their PRODUCED colour, so G stays wrong.

    (This test previously asserted the C, under the name "shows no
    badge". It was pinning the bug.)
    """
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Forest", color="",
                 type_line="Basic Land — Forest")
    response = TestClient(main.app).get("/inventory?show_all=true&status_submitted=1")
    assert response.status_code == 200
    assert 'color-pip-l" title="Land">L</span>' in response.text
    assert 'color-pip-c" title="Colorless">C</span>' not in response.text
    assert 'color-pip-g" title="Green">G</span>' not in response.text
