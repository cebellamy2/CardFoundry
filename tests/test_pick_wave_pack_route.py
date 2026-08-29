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
    db = create_engine(f"sqlite:///{tmp_path / 'wave-pack.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_picked_order(session, *, source="manapool"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        source=source, status="picked",
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
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="picked",
    )
    session.add(allocation)
    session.flush()
    return order, allocation


def make_wave(session, orders, *, status="active"):
    wave = PickWave(label="Wave", status=status)
    session.add(wave)
    session.flush()
    membership_status = "closed" if status == "completed" else "active"
    for order in orders:
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status=membership_status))
    session.flush()
    return wave


def test_pick_wave_page_shows_mark_wave_as_packed_for_picked_orders(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_picked_order(session)
        wave = make_wave(session, [order])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert f'action="/pick-waves/{wave_id}/pack"' in page.text
    assert "Mark Wave as Packed" in page.text


def test_pick_wave_page_omits_pack_button_when_nothing_to_pack(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = PickWave(label="Empty Wave", status="active")
        session.add(wave)
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert "Mark Wave as Packed" not in page.text


def test_pack_route_packs_every_picked_order_in_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order_a, allocation_a = make_picked_order(session)
        order_b, allocation_b = make_picked_order(session)
        wave = make_wave(session, [order_a, order_b])
        session.commit()
        wave_id, id_a, id_b = wave.id, order_a.id, order_b.id
        alloc_a, alloc_b = allocation_a.id, allocation_b.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/pack")
    assert response.status_code == 200
    assert "Succeeded: <strong>2</strong>" in response.text
    assert "Skipped: <strong>0</strong>" in response.text
    assert f'href="/pick-waves/{wave_id}"' in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, id_a).status == "packed"
        assert session.get(SalesOrder, id_b).status == "packed"
        assert session.get(PickAllocation, alloc_a).status == "packed"
        assert session.get(PickAllocation, alloc_b).status == "packed"


def test_pack_route_works_on_a_completed_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_picked_order(session)
        wave = make_wave(session, [order], status="completed")
        session.commit()
        wave_id, order_id = wave.id, order.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/pack")
    assert response.status_code == 200
    assert "Succeeded: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "packed"


def test_pack_route_is_batch_isolated(tmp_path, monkeypatch):
    """A non-manapool order still at 'picked' passes the pre-filter but
    fails the source check inside the shared pack transition -- confirms
    one order's failure doesn't block its sibling in the same wave.
    """
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        eligible, _ = make_picked_order(session)
        wrong_source, _ = make_picked_order(session, source="simulation")
        wave = make_wave(session, [eligible, wrong_source])
        session.commit()
        wave_id, eligible_id, wrong_source_id = wave.id, eligible.id, wrong_source.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/pack")
    assert response.status_code == 200
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Skipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, eligible_id).status == "packed"
        assert session.get(SalesOrder, wrong_source_id).status == "picked"


def test_pack_route_silently_excludes_orders_not_currently_picked(tmp_path, monkeypatch):
    """No explicit selection step exists for the wave-level pack action --
    it should only ever attempt orders actually at 'picked', not report a
    skip for one that's already moved past it.
    """
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        eligible, _ = make_picked_order(session)
        already_packed, _ = make_picked_order(session)
        already_packed.status = "packed"
        wave = make_wave(session, [eligible, already_packed])
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/pack")
    assert response.status_code == 200
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Skipped: <strong>0</strong>" in response.text


def test_pack_route_404s_for_missing_wave(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/pick-waves/999999/pack")
    assert response.status_code == 404


def test_pack_route_400s_when_nothing_to_pack(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = PickWave(label="Empty Wave", status="active")
        session.add(wave)
        session.commit()
        wave_id = wave.id

    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/pack")
    assert response.status_code == 400
