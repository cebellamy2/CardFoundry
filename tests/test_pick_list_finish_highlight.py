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


def make_wave_with_card(session, *, finish, batch_code="B1"):
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
        condition_id="LP", finish_id="NF", finish=finish, status="reserved",
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
        wave = make_wave_with_card(session, finish="normal")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="non-normal-finish"' not in page.text


def test_foil_finish_row_gets_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="foil")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert '<tr class="non-normal-finish">' in page.text


def test_etched_finish_row_gets_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish="etched")
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert '<tr class="non-normal-finish">' in page.text


def test_blank_finish_row_has_no_highlight_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave_with_card(session, finish=None)
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="non-normal-finish"' not in page.text
