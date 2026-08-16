from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PickWave, PickWaveEvent, PickWaveOrder, SalesOrder
from tests.test_pick_wave_routes import make_active_wave, make_order_with_active_allocation


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'reopen_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(main, "update_seller_order_fulfillment", lambda *a, **k: {})
    return db


def complete_wave(client, wave_id):
    response = client.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code == 303


def test_completed_wave_page_shows_reopen_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    complete_wave(client, wave_id)

    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert f'action="/pick-waves/{wave_id}/reopen"' in page.text
    assert "Reopen Pick Wave" in page.text


def test_active_wave_page_does_not_show_reopen_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert "Reopen Pick Wave" not in page.text


def test_reopen_route_reverts_a_clean_completed_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    complete_wave(client, wave_id)

    response = client.post(f"/pick-waves/{wave_id}/reopen", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/pick-waves/{wave_id}"

    with Session(db) as session:
        assert session.get(PickWave, wave_id).status == "active"
        assert session.get(SalesOrder, order_id).status == "in_pick_wave"
        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave_id, PickWaveOrder.order_id == order_id,
        ).one()
        assert membership.status == "active"
        event = session.query(PickWaveEvent).filter(
            PickWaveEvent.pick_wave_id == wave_id,
        ).one()
        assert event.event_type == "reopened"


def test_reopen_route_shows_history_banner_after_success(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    complete_wave(client, wave_id)
    client.post(f"/pick-waves/{wave_id}/reopen", follow_redirects=False)

    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert "reopened 1 time" in page.text
    assert "Mana Pool has already been told" in page.text


def test_reopen_route_fails_closed_when_an_order_already_packed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    complete_wave(client, wave_id)

    packed = client.post(f"/orders/{order_id}/packed", follow_redirects=False)
    assert packed.status_code == 303

    response = client.post(f"/pick-waves/{wave_id}/reopen")
    assert response.status_code == 409
    assert "not picked" in response.text

    with Session(db) as session:
        assert session.get(PickWave, wave_id).status == "completed"
        assert session.get(SalesOrder, order_id).status == "packed"


def test_reopen_route_rejects_active_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/reopen")
    assert response.status_code == 409
    assert "completed" in response.text


def test_reopen_route_rejects_cancelled_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_order_with_active_allocation(session)
        wave = make_active_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    cancelled = client.post(f"/pick-waves/{wave_id}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303

    response = client.post(f"/pick-waves/{wave_id}/reopen")
    assert response.status_code == 409


def test_reopen_route_404_for_missing_wave(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/pick-waves/999999/reopen")
    assert response.status_code == 404
