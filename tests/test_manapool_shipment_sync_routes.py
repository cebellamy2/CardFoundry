import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'shipment_sync.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_packed_order(session, *, source="manapool", external_order_id="mp-order-1"):
    order = SalesOrder(
        external_order_id=external_order_id,
        source=source,
        status="packed",
    )
    session.add(order)
    session.flush()
    return order


def test_shipping_a_manapool_order_pushes_status_and_marks_synced(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_packed_order(session)
        session.commit()
        order_id, external_id = order.id, order.external_order_id

    calls = []

    def fake_update(order_id_arg, status, tracking_number=None, tracking_company=None, tracking_url=None):
        calls.append({
            "order_id": order_id_arg,
            "status": status,
            "tracking_number": tracking_number,
            "tracking_company": tracking_company,
            "tracking_url": tracking_url,
        })
        return {"fulfillment": {"status": "shipped"}}

    monkeypatch.setattr(main, "update_seller_order_fulfillment", fake_update)

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/shipped",
        data={"tracking_number": "1Z999AA10123456784"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert calls == [{
        "order_id": external_id,
        "status": "shipped",
        "tracking_number": "1Z999AA10123456784",
        "tracking_company": "usps",
        "tracking_url": None,
    }]

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "shipped"
        assert refreshed.mana_pool_shipment_synced_at is not None


def test_push_failure_leaves_order_shipped_but_unsynced(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_packed_order(session)
        session.commit()
        order_id = order.id

    def failing_update(*args, **kwargs):
        raise httpx.HTTPError("network down")

    monkeypatch.setattr(main, "update_seller_order_fulfillment", failing_update)

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/shipped",
        data={"tracking_number": "1Z999"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "shipped"
        assert refreshed.mana_pool_shipment_synced_at is None


def test_order_released_leaves_order_shipped_but_unsynced_and_does_not_raise(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_packed_order(session)
        session.commit()
        order_id = order.id

    monkeypatch.setattr(
        main,
        "update_seller_order_fulfillment",
        lambda *a, **k: {"released": True, "message": "already refunded"},
    )

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/shipped",
        data={"tracking_number": "1Z999"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "shipped"
        assert refreshed.mana_pool_shipment_synced_at is None


def test_simulated_order_never_calls_manapool(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_packed_order(session, source="simulation", external_order_id="TEST-001")
        session.commit()
        order_id = order.id

    calls = []
    monkeypatch.setattr(
        main,
        "update_seller_order_fulfillment",
        lambda *a, **k: calls.append((a, k)),
    )

    client = TestClient(main.app)
    response = client.post(
        f"/orders/{order_id}/shipped",
        data={"tracking_number": "1Z999"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == []

    with Session(db) as session:
        refreshed = session.get(SalesOrder, order_id)
        assert refreshed.status == "shipped"
        assert refreshed.mana_pool_shipment_synced_at is None
