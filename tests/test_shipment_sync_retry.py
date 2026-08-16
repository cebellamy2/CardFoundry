from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'shipment_sync_retry.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_stuck_order(session, *, failure_detail="network down"):
    order = SalesOrder(
        external_order_id="mp-order-stuck",
        source="manapool",
        status="shipped",
        tracking_number="1Z999",
        shipped_at=datetime(2026, 8, 15, 9, 0, 0),
        mana_pool_shipment_failure_detail=failure_detail,
    )
    session.add(order)
    session.flush()
    return order


def test_retry_succeeds_and_clears_failure_detail(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_stuck_order(session)
        session.commit()
        order_id, external_id = order.id, order.external_order_id

    calls = []

    def fake_update(order_id_arg, status, tracking_number=None, tracking_company=None, tracking_url=None):
        calls.append(order_id_arg)
        return {"fulfillment": {"status": "shipped"}}

    monkeypatch.setattr(main, "update_seller_order_fulfillment", fake_update)

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-shipment-sync", follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/orders/shipment-sync-issues"
    assert calls == [external_id]

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.mana_pool_shipment_synced_at is not None
        assert refreshed.mana_pool_shipment_failure_detail is None


def test_retry_records_new_failure_detail_on_repeated_failure(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_stuck_order(session, failure_detail="first failure")
        session.commit()
        order_id = order.id

    import httpx

    def failing_update(*args, **kwargs):
        raise httpx.HTTPError("still down")

    monkeypatch.setattr(main, "update_seller_order_fulfillment", failing_update)

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-shipment-sync", follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.mana_pool_shipment_synced_at is None
        assert refreshed.mana_pool_shipment_failure_detail == "still down"


def test_retry_does_not_re_run_mark_shipped(tmp_path, monkeypatch):
    """Retrying must not touch shipped_at or re-run local allocation/card
    transitions -- it only re-attempts the remote push."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_stuck_order(session)
        session.commit()
        order_id = order.id
        original_shipped_at = order.shipped_at

    monkeypatch.setattr(
        main, "update_seller_order_fulfillment",
        lambda *a, **k: {"fulfillment": {"status": "shipped"}},
    )

    client = TestClient(main.app)
    client.post(f"/orders/{order_id}/retry-shipment-sync", follow_redirects=False)

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.shipped_at == original_shipped_at


def test_retry_is_a_noop_for_an_already_synced_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_stuck_order(session, failure_detail=None)
        order.mana_pool_shipment_synced_at = datetime(2026, 8, 15, 9, 5, 0)
        session.add(order)
        session.commit()
        order_id = order.id

    calls = []
    monkeypatch.setattr(
        main, "update_seller_order_fulfillment",
        lambda *a, **k: calls.append(1),
    )

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-shipment-sync", follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == []


def test_retry_is_a_noop_for_a_non_manapool_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="TEST-1", source="simulation", status="shipped",
            shipped_at=datetime(2026, 8, 15, 9, 0, 0),
        )
        session.add(order)
        session.commit()
        order_id = order.id

    calls = []
    monkeypatch.setattr(
        main, "update_seller_order_fulfillment",
        lambda *a, **k: calls.append(1),
    )

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/retry-shipment-sync", follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == []


def test_retry_route_handles_missing_order(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/orders/999/retry-shipment-sync", follow_redirects=False,
    )
    assert response.status_code == 303


def test_banner_appears_when_an_order_is_stuck(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_stuck_order(session)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "failed to sync to Mana Pool" in response.text
    assert "/orders/shipment-sync-issues" in response.text


def test_banner_is_absent_when_nothing_is_stuck(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200
    assert "failed to sync to Mana Pool" not in response.text


def test_control_page_lists_stuck_orders_with_failure_detail(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_stuck_order(session, failure_detail="connection reset")
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "connection reset" in response.text
    assert "mp-order-stuck" in response.text
    assert "Retry Now" in response.text


def test_control_page_shows_empty_state_when_nothing_stuck(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "No orders currently have a stuck Mana Pool status sync." in response.text


def test_control_page_excludes_released_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_stuck_order(session, failure_detail=None)
        order.mana_pool_shipment_released_at = datetime(2026, 8, 15, 9, 5, 0)
        order.mana_pool_shipment_release_detail = "already refunded"
        session.add(order)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "No orders currently have a stuck Mana Pool status sync." in response.text
