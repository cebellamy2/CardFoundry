from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'orders-page-ui.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_order(session, external_order_id, status):
    order = SalesOrder(external_order_id=external_order_id, status=status)
    session.add(order)
    session.flush()
    return order


# --- default visibility ---

def test_bare_load_hides_cancelled_and_shipped_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "needs-review-1" in response.text
    assert "cancelled-1" not in response.text
    assert "shipped-1" not in response.text


def test_explicit_cancelled_filter_still_shows_cancelled_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "cancelled-1", "cancelled")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=cancelled")
    assert response.status_code == 200
    assert "cancelled-1" in response.text
    assert "needs-review-1" not in response.text


def test_explicit_shipped_filter_still_shows_shipped_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=shipped")
    assert response.status_code == 200
    assert "shipped-1" in response.text


def test_all_tab_count_excludes_cancelled_and_shipped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        make_order(session, "short-1", "short")
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "All (2)" in response.text


def test_cancelled_and_shipped_tabs_still_appear_for_pulling_back_up(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "cancelled-1", "cancelled")
        make_order(session, "shipped-1", "shipped")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'href="/orders?status=cancelled"' in response.text
    assert 'href="/orders?status=shipped"' in response.text


# --- pill tab styling ---

def test_status_tabs_use_pill_tab_classes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert 'class="status-tabs no-print"' in response.text
    assert 'class="status-tab active"' in response.text
    assert 'class="status-tab"' in response.text


def test_active_tab_reflects_current_status_filter(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_order(session, "needs-review-1", "needs_review")
        session.commit()
    client = TestClient(main.app)
    response = client.get("/orders?status=needs_review")
    assert response.status_code == 200
    assert 'href="/orders?status=needs_review"' in response.text
    active_tab_start = response.text.index('href="/orders?status=needs_review"')
    preceding_text = response.text[max(0, active_tab_start - 80):active_tab_start]
    assert "active" in preceding_text
    # The "All" tab must not also be marked active when a status filter is applied.
    assert '<a class="status-tab active" href="/orders">' not in response.text


# --- removed headings ---

def test_fulfillment_queue_heading_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "Fulfillment Queue" not in response.text


def test_existing_orders_heading_is_removed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "Existing Orders" not in response.text


def test_orders_h1_is_still_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "<h1>" in response.text and "Orders" in response.text
