from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


def setup_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'color_backfill_route.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def add_card(session, *, scryfall_id, name="Forest"):
    batch = Batch(batch_code=f"B-{scryfall_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, scryfall_id=scryfall_id,
        color=None, status="available",
    )
    session.add(card)
    session.flush()
    return card


def add_item(session, *, scryfall_id, name="Forest"):
    order = SalesOrder(external_order_id=f"o-{scryfall_id}", source="manapool", status="shipped")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=name, scryfall_id=scryfall_id, color=None, quantity=1)
    session.add(item)
    session.flush()
    return item


def fake_scryfall_lookup(ids):
    data = {"sf-bolt": {"id": "sf-bolt", "name": "Lightning Bolt", "colors": ["R"]}}
    return {sid: data[sid] for sid in ids if sid in data}


def test_route_backfills_and_reports_counts(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, scryfall_id="sf-bolt")
        add_item(session, scryfall_id="sf-bolt")
        session.commit()

    monkeypatch.setattr(main, "fetch_scryfall_cards", fake_scryfall_lookup)
    client = TestClient(main.app)
    response = client.post("/admin/color-backfill")

    assert response.status_code == 200
    assert "Inventory cards backfilled: <strong>1</strong>" in response.text
    assert "Order items backfilled: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).one().color == "R"
        assert session.query(OrderItem).one().color == "R"


def test_route_reports_unresolved_scryfall_ids(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, scryfall_id="sf-unknown")
        session.commit()

    monkeypatch.setattr(main, "fetch_scryfall_cards", fake_scryfall_lookup)
    client = TestClient(main.app)
    response = client.post("/admin/color-backfill")

    assert response.status_code == 200
    assert "sf-unknown" in response.text
    assert "could not be" in response.text


def test_route_is_a_clean_noop_when_nothing_needs_backfilling(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "fetch_scryfall_cards", fake_scryfall_lookup)
    client = TestClient(main.app)
    response = client.post("/admin/color-backfill")

    assert response.status_code == 200
    assert "Inventory cards backfilled: <strong>0</strong>" in response.text
    assert "Order items backfilled: <strong>0</strong>" in response.text


def test_admin_page_links_to_manual_backfill(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'action="/admin/color-backfill"' in response.text
