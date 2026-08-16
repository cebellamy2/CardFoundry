from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PickAllocation, SalesOrder
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'bulk-pack.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_picked_order(session, source="manapool"):
    order, item, card, allocation = seed(
        session, allocation_status="picked", card_status="reserved",
        order_status="picked",
    )
    order.source = source
    session.commit()
    return order, allocation


def test_orders_page_shows_bulk_pack_checkbox_for_picked_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_picked_order(session)
        order_id = order.id
    client = TestClient(main.app)
    page = client.get("/orders")
    assert page.status_code == 200
    assert 'action="/orders/bulk-pack"' in page.text
    assert 'name="pack_order_ids"' in page.text
    assert f'value="{order_id}"' in page.text


def test_bulk_pack_packs_multiple_eligible_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order_a, allocation_a = make_picked_order(session)
        order_b, allocation_b = make_picked_order(session)
        ids = [order_a.id, order_b.id]
        allocation_ids = [allocation_a.id, allocation_b.id]

    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={"pack_order_ids": ids})
    assert response.status_code == 200
    assert "Packed: <strong>2</strong>" in response.text
    assert "Skipped: <strong>0</strong>" in response.text

    with Session(db) as session:
        for order_id in ids:
            order = session.get(SalesOrder, order_id)
            assert order.status == "packed"
            assert order.packed_at is not None
        for allocation_id in allocation_ids:
            assert session.get(PickAllocation, allocation_id).status == "packed"


def test_bulk_pack_is_batch_isolated_one_order_does_not_block_others(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        eligible, eligible_allocation = make_picked_order(session)
        already_packed, _ = make_picked_order(session)
        already_packed.status = "packed"
        session.commit()
        ids = [eligible.id, already_packed.id]
        eligible_id, ineligible_id = eligible.id, already_packed.id
        eligible_allocation_id = eligible_allocation.id

    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={"pack_order_ids": ids})
    assert response.status_code == 200
    assert "Packed: <strong>1</strong>" in response.text
    assert "Skipped: <strong>1</strong>" in response.text
    assert "not picked" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, eligible_id).status == "packed"
        assert session.get(PickAllocation, eligible_allocation_id).status == "packed"
        assert session.get(SalesOrder, ineligible_id).status == "packed"


def test_bulk_pack_revalidates_source_at_write_time(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_picked_order(session, source="other_marketplace")
        order_id = order.id

    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={"pack_order_ids": [order_id]})
    assert response.status_code == 200
    assert "Packed: <strong>0</strong>" in response.text
    assert "not manapool" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "picked"


def test_bulk_pack_with_no_selection_shows_friendly_message(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={})
    assert response.status_code == 400
    assert "No orders selected" in response.text


def test_bulk_pack_missing_order_id_is_reported_not_raised(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={"pack_order_ids": [999999]})
    assert response.status_code == 200
    assert "Skipped: <strong>1</strong>" in response.text
    assert "Order not found" in response.text
