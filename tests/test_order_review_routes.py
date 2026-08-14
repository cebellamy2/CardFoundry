from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'review_routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_needs_review_order_shows_review_detail_and_approve_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="mp-1",
            source="manapool",
            status="needs_review",
            review_detail="Mana Pool order line 'Weird Card' lacks canonical identity.",
        )
        session.add(order)
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert "lacks canonical identity" in page.text
    assert "Approve &amp; Allocate Inventory" in page.text or "Approve & Allocate Inventory" in page.text


def test_short_order_shows_retry_button_and_no_review_detail_leak(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="mp-2",
            source="manapool",
            status="short",
        )
        session.add(order)
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert "Retry Allocation" in page.text
    assert "short on" in page.text
    assert "matching" in page.text
    assert "stock" in page.text


def test_approve_route_accepts_short_status_and_retries_allocation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
            mtgjson_id="MTG-ALPHA", language_id="EN", condition_id="LP", finish_id="NF",
            condition="near_mint", finish="normal", status="available",
        )
        session.add(card)
        session.flush()
        order = SalesOrder(external_order_id="mp-3", source="manapool", status="short")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
            condition_id="LP", finish_id="NF", quantity=1,
        ))
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/approve", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "ready_to_pick"


def test_approve_route_rejects_orders_not_in_review_or_short(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(external_order_id="mp-4", source="manapool", status="ready_to_pick")
        session.add(order)
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/approve", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "ready_to_pick"
