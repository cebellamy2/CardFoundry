from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pick-list-finish.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_wave_with_card(session, *, finish, finish_id="NF", batch_code="B1"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        status="in_pick_wave",
    )
    session.add(order)
    session.flush()
    batch = Batch(batch_code=batch_code)
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
        condition_id="LP", finish_id=finish_id, finish=finish, status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name="Alpha", quantity=1)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
    ))
    wave = PickWave(label="Wave", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.flush()
    return wave


def test_normal_finish_row_has_no_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="normal", finish_id="NF")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="non-normal-finish"' not in page.text


def test_foil_finish_row_gets_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="foil", finish_id="FO")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert '<tr class="non-normal-finish">' in page.text


def test_etched_finish_row_gets_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="etched", finish_id="EF")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert '<tr class="non-normal-finish">' in page.text


def test_blank_finish_row_has_no_highlight_class(tmp_path, monkeypatch):
    """finish=NULL, finish_id='NF' -- the realistic shape for most of the
    814 NULL-finish rows found in production (644 of them are exactly
    this pairing). Must stay unbolded."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish=None, finish_id="NF")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="non-normal-finish"' not in page.text


# --- 2026-09-01: highlight now falls back to finish_id, same as display --

def test_null_finish_etched_finish_id_now_gets_highlighted(tmp_path, monkeypatch):
    """The bug this slice fixes: finish=NULL, finish_id='EF' used to show
    "Etched" in the Finish column text but render with no bold highlight
    at all -- the display already fell back to finish_id, the highlight
    check didn't. Constructed rather than found live: the 3 real
    production rows in this exact shape are all status='sold' and never
    reach a pick list."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish=None, finish_id="EF")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert "Etched" in page.text
    assert '<tr class="non-normal-finish">' in page.text


def test_null_finish_and_null_finish_id_still_has_no_highlight_class(tmp_path, monkeypatch):
    """Presence check must survive the fallback: a card with neither
    field set renders unbolded, not a crash and not a false positive."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish=None, finish_id=None)
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="non-normal-finish"' not in page.text


def test_finish_display_and_highlight_agree_on_the_same_row(tmp_path, monkeypatch):
    """One concept, one source of truth: the Finish column text and the
    row's bold state must never disagree for the same card."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish=None, finish_id="FO")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    tr_idx = page.text.rindex("<tr", 0, page.text.index("Alpha"))
    row = page.text[tr_idx:tr_idx + 400]
    assert "non-normal-finish" in row
    assert "Foil" in row
