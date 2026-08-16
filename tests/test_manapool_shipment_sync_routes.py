import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder


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


def make_packed_order_with_card(session, *, price_cents=1999, source="manapool"):
    order = make_packed_order(session, source=source)
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
        condition_id="LP", finish_id="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(
        order_id=order.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
        condition_id="LP", finish_id="NF", quantity=1, price_cents=price_cents,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="packed",
    )
    session.add(allocation)
    session.flush()
    return order, card


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
        assert refreshed.mana_pool_shipment_released_at is None


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
        assert refreshed.mana_pool_shipment_released_at is None
        assert refreshed.mana_pool_shipment_failure_detail == "network down"


def test_sold_price_is_captured_regardless_of_mana_pool_push_outcome(tmp_path, monkeypatch):
    """A card that shipped and sold locally is sold regardless of whether
    the Mana Pool status push succeeds, fails, or is retried later."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, card = make_packed_order_with_card(session, price_cents=1999)
        session.commit()
        order_id, card_id = order.id, card.id

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
        refreshed_card = session.get(InventoryCard, card_id)
        assert refreshed_card.status == "sold"
        assert refreshed_card.sold_price == 19.99
        refreshed_order = session.get(SalesOrder, order_id)
        assert refreshed_order.mana_pool_shipment_synced_at is None


def test_order_released_is_recorded_distinctly_and_does_not_raise(tmp_path, monkeypatch):
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
        # A genuine push failure and an order_released response used to
        # look identical (both left mana_pool_shipment_synced_at NULL). A
        # future retry job needs to tell them apart: released is
        # terminal and must never be retried.
        assert refreshed.mana_pool_shipment_synced_at is None
        assert refreshed.mana_pool_shipment_released_at is not None
        assert refreshed.mana_pool_shipment_release_detail == "already refunded"


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
        assert refreshed.mana_pool_shipment_released_at is None
