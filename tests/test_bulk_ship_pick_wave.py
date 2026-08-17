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
    db = create_engine(f"sqlite:///{tmp_path / 'bulk-ship.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_packed_order(session, *, shipping_method=None, source="manapool"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        source=source, status="packed", shipping_method=shipping_method,
    )
    session.add(order)
    session.flush()
    batch = Batch(batch_code=f"B-{order.id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id="MTG-ALPHA", language_id="EN",
        condition_id="LP", finish_id="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name="Alpha", quantity=1)
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="packed",
    )
    session.add(allocation)
    session.flush()
    return order, allocation


def make_wave(session, orders):
    wave = PickWave(label="Wave", status="active")
    session.add(wave)
    session.flush()
    for order in orders:
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.flush()
    return wave


def make_completed_wave(session, orders):
    """A wave whose membership rows are 'closed', as complete_pick_wave() leaves them.

    Packing an order (bulk-pack, off the /orders page) never touches wave
    membership -- only completion (closed) and explicit single-order
    removal (removed) do. A packed order belonging to a completed wave
    should still show up on that wave's page: it never left the wave, the
    wave just moved on without it being physically re-picked again.
    """
    wave = PickWave(label="Wave", status="completed")
    session.add(wave)
    session.flush()
    for order in orders:
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="closed"))
    session.flush()
    return wave


def test_pick_wave_page_shows_shipping_address_for_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="ground_advantage")
        order.shipping_name = "Jane Doe"
        order.shipping_line1 = "123 Main St"
        order.shipping_city = "Springfield"
        order.shipping_state = "IL"
        order.shipping_postal_code = "62704"
        wave = make_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert "Jane Doe" in page.text
    assert "123 Main St" in page.text
    assert "Copy Address" in page.text


def test_pick_wave_page_highlights_tracking_required_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="ground_advantage")
        wave = make_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert '<tr class="tracking-required">' in page.text


def test_pick_wave_page_does_not_highlight_orders_that_dont_require_tracking(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="first_class")
        wave = make_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'class="tracking-required"' not in page.text


def test_pick_wave_page_shows_tracking_input_for_packed_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="ground_advantage")
        wave = make_wave(session, [order])
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'action="/pick-waves/' in page.text and '/ship"' in page.text
    assert f'name="ship_order_ids" value="{order_id}"' in page.text
    assert "Tracking # (required)" in page.text
    assert "Mark Wave as Shipped" in page.text


def test_bulk_ship_succeeds_when_no_tracking_required(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, allocation = make_packed_order(session, shipping_method="first_class", source="simulation")
        wave = make_wave(session, [order])
        session.commit()
        wave_id, order_id, allocation_id = wave.id, order.id, allocation.id

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/ship",
        data={"ship_order_ids": [order_id], "tracking_numbers": [""]},
    )
    assert response.status_code == 200
    assert "Shipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "shipped"
        assert order.shipped_at is not None
        assert session.get(PickAllocation, allocation_id).status == "shipped"


def test_bulk_ship_blocks_entire_batch_when_tracking_required_and_missing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        needs_tracking, _ = make_packed_order(session, shipping_method="ground_advantage", source="simulation")
        no_tracking_needed, _ = make_packed_order(session, shipping_method="first_class", source="simulation")
        wave = make_wave(session, [needs_tracking, no_tracking_needed])
        session.commit()
        wave_id = wave.id
        needs_id, other_id = needs_tracking.id, no_tracking_needed.id

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/ship",
        data={
            "ship_order_ids": [needs_id, other_id],
            "tracking_numbers": ["", ""],
        },
    )
    assert response.status_code == 400
    assert "Tracking numbers required" in response.text

    with Session(db) as session:
        # Neither order shipped -- all-or-nothing.
        assert session.get(SalesOrder, needs_id).status == "packed"
        assert session.get(SalesOrder, other_id).status == "packed"


def test_bulk_ship_succeeds_with_tracking_provided_for_required_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="ground_advantage", source="simulation")
        wave = make_wave(session, [order])
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/ship",
        data={"ship_order_ids": [order_id], "tracking_numbers": ["1Z999AA10123456784"]},
    )
    assert response.status_code == 200
    assert "Shipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "shipped"
        assert order.tracking_number == "1Z999AA10123456784"


def test_completed_wave_still_shows_packed_orders(tmp_path, monkeypatch):
    """Regression: complete_pick_wave() closes wave membership, and the
    wave page used to default to active-only, so a packed order silently
    vanished from its own wave's page the moment the wave completed --
    exactly the scenario a real production wave (34 orders, all packed)
    hit. Packing doesn't touch wave membership; an order should stay
    visible on the wave it belongs to through its whole lifecycle.
    """
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="ground_advantage")
        wave = make_completed_wave(session, [order])
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert f'name="ship_order_ids" value="{order_id}"' in page.text
    assert "Mark Wave as Shipped" in page.text


def test_bulk_ship_works_on_a_completed_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, allocation = make_packed_order(
            session, shipping_method="first_class", source="simulation",
        )
        wave = make_completed_wave(session, [order])
        session.commit()
        wave_id, order_id, allocation_id = wave.id, order.id, allocation.id

    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/ship",
        data={"ship_order_ids": [order_id], "tracking_numbers": [""]},
    )
    assert response.status_code == 200
    assert "Shipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "shipped"
        assert session.get(PickAllocation, allocation_id).status == "shipped"


def test_removed_order_does_not_reappear_on_wave_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_packed_order(session, shipping_method="first_class")
        wave = make_wave(session, [order])
        session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave.id, PickWaveOrder.order_id == order.id,
        ).one().status = "removed"
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert f'name="ship_order_ids" value="{order_id}"' not in page.text
