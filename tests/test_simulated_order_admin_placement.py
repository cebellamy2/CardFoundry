from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'simulated-order-admin.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_admin_page_links_to_create_simulated_order(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/simulated-order"' in response.text


def test_orders_page_no_longer_shows_the_simulated_order_form(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "Create Simulated Order" not in response.text
    assert 'action="/orders/create"' not in response.text


def test_simulated_order_form_page_renders(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin/simulated-order")
    assert response.status_code == 200
    assert "Create Simulated Order" in response.text
    assert 'action="/orders/create"' in response.text
    assert 'name="order_reference"' in response.text
    assert 'name="items_text"' in response.text
    assert 'href="/admin"' in response.text


def test_simulated_order_submit_still_reaches_allocation(tmp_path, monkeypatch):
    """Moving the form off /orders must not change the POST handler's own
    behavior -- with no matching inventory seeded, allocation correctly
    blocks and rolls back, same as before the move."""
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/orders/create",
        data={"order_reference": "TEST-003", "items_text": "Alpha | ONE | 1 | normal | 1"},
    )
    assert response.status_code == 409
    assert "Order allocation blocked" in response.text
    with Session(db) as session:
        assert session.query(SalesOrder).filter_by(external_order_id="TEST-003").first() is None
