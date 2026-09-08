from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import SalesOrder
from tests.test_order_detail_item13_redesign import make_order_with_allocation, setup_db


# CF-UNDO-002 item 2: uncancel route + order-detail wiring.

def test_order_detail_shows_uncancel_button_when_snapshot_exists(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    client = TestClient(main.app)
    client.post(f"/orders/{order_id}/cancel")
    response = client.get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/uncancel"' in response.text
    assert "Uncancel" in response.text


def test_order_detail_shows_no_uncancel_button_without_snapshot(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="cancelled")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/uncancel"' not in response.text
    assert "no restore snapshot" in response.text


def test_uncancel_route_success_restores_order_and_shows_outcome(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    client = TestClient(main.app)
    client.post(f"/orders/{order_id}/cancel")
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"

    response = client.post(f"/orders/{order_id}/uncancel", follow_redirects=False)
    assert response.status_code == 200
    assert "Order Uncancelled" in response.text
    assert "cancelled → ready_to_pick" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"


def test_uncancel_route_refused_unless_cancelled(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/uncancel")
    assert response.status_code == 409
    assert "Uncancel Refused" in response.text


def test_uncancel_route_404_for_missing_order(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/orders/999/uncancel")
    assert response.status_code == 404
