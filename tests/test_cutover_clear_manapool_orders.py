from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'cutover.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def make_order(session, *, status="needs_review", source="manapool", external_id="mp-1"):
    order = SalesOrder(external_order_id=external_id, source=source, status=status)
    session.add(order); session.flush()
    item = OrderItem(order_id=order.id, name="Lightning Bolt", quantity=1)
    session.add(item); session.commit()
    return order, item


def test_clear_marks_safe_order_cleared_and_preserves_order_and_items(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item = make_order(session)
        order_id, item_id = order.id, item.id

    client = TestClient(main.app)
    response = client.post("/cutover/clear-manapool-orders")
    assert response.status_code == 200
    assert "Pre-Cutover Orders Cleared" in response.text

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order is not None
        assert order.status == "cleared"
        assert order.cleared_from_status == "needs_review"
        assert order.cleared_at is not None
        assert session.get(OrderItem, item_id) is not None


def test_clear_protects_orders_with_active_allocations(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item = make_order(session)
        batch = Batch(batch_code="PROD", is_archived=False)
        session.add(batch); session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Lightning Bolt", set_code="SET", collector_number="1",
            scryfall_id="sf-1", mtgjson_id="mtg-1", language_id="EN",
            condition_id="LP", finish_id="NF", condition="LP", finish="normal",
            status="reserved",
        )
        session.add(card); session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
        ))
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    response = client.post("/cutover/clear-manapool-orders")
    assert response.status_code == 200

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "needs_review"


def test_clear_protects_orders_in_a_pick_wave(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item = make_order(session)
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    client.post("/cutover/clear-manapool-orders")

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "needs_review"


def test_clear_skips_orders_already_cleared(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_order(session)
        order.status = "cleared"
        order.cleared_from_status = "needs_review"
        session.commit()
        order_id = order.id

    client = TestClient(main.app)
    response = client.post("/cutover/clear-manapool-orders")
    assert response.status_code == 200
    assert "Cleared in CardFoundry only:" in response.text
    assert "<strong>0</strong>" in response.text

    with Session(db) as session:
        # Untouched -- not re-processed, not double-counted.
        assert session.get(SalesOrder, order_id).status == "cleared"


def test_un_clear_restores_prior_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_order(session, status="short")
        order_id = order.id
        session.commit()

    client = TestClient(main.app)
    client.post("/cutover/clear-manapool-orders")
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cleared"

    response = client.post(f"/cutover/un-clear-order/{order_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/cutover"

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "short"
        assert order.cleared_at is None
        assert order.cleared_from_status is None
        assert order.cleared_note is None


def test_un_clear_refused_for_order_not_cleared(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_order(session)
        order_id = order.id

    client = TestClient(main.app)
    response = client.post(f"/cutover/un-clear-order/{order_id}")
    assert response.status_code == 409
    assert "not currently cleared" in response.text

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "needs_review"


def test_un_clear_refused_for_nonexistent_order(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/cutover/un-clear-order/999")
    assert response.status_code == 409


def test_cutover_page_excludes_cleared_orders_from_counts_and_lists_them_separately(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _ = make_order(session, external_id="mp-cleared")
        order.status = "cleared"
        order.cleared_from_status = "needs_review"
        from datetime import datetime
        order.cleared_at = datetime.now()
        session.commit()
        make_order(session, external_id="mp-open")
        session.commit()

    client = TestClient(main.app)
    page = client.get("/cutover")
    assert page.status_code == 200
    # The open order counts toward "Mana Pool orders currently in
    # CardFoundry" / safe-to-clear; the cleared one does not.
    assert "Mana Pool orders currently in CardFoundry:\n            <strong>1</strong>" in page.text
    assert "Safe to clear:\n            <strong>1</strong>" in page.text
    assert "mp-cleared" in page.text and "mp-open" not in page.text
    assert 'action="/cutover/un-clear-order/' in page.text
    assert "Un-clear" in page.text
