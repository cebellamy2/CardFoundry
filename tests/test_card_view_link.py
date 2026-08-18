from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'card_view_link.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, name="Forest", scryfall_id="sf-1", status="available"):
    batch = Batch(batch_code=f"B-{name}-{scryfall_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="SET", collector_number="1",
        scryfall_id=scryfall_id, condition="LP", condition_id="LP", finish="normal",
        finish_id="NF", language_id="EN", status=status,
    )
    session.add(card)
    session.commit()
    return card


def test_view_link_renders_a_button_linked_to_full_size_image():
    html = main._card_view_link("sf-abc123")
    assert html == (
        '<a href="https://api.scryfall.com/cards/sf-abc123?format=image&version=large" '
        'target="_blank" rel="noopener" class="card-view-link">View Card</a>'
    )


def test_view_link_is_empty_when_scryfall_id_missing():
    assert main._card_view_link(None) == ""
    assert main._card_view_link("") == ""


def test_detail_table_escapes_normal_values_but_not_raw_html_labels():
    html = main._detail_table_html(
        {"Card": "<b>trusted</b>", "Note": "<script>alert(1)</script>"},
        raw_html_labels=frozenset({"Card"}),
    )
    assert "<b>trusted</b>" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_detail_table_escapes_everything_with_no_raw_labels():
    html = main._detail_table_html({"Card": "<b>x</b>"})
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_inventory_search_shows_view_card_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, scryfall_id="sf-forest")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert 'href="https://api.scryfall.com/cards/sf-forest?format=image&version=large" ' \
        'target="_blank" rel="noopener" class="card-view-link">View Card</a>' in response.text


def test_inventory_search_no_button_for_missing_scryfall_id(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="NOID")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Mystery Card", scryfall_id=None, status="available",
        ))
        session.commit()
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "Mystery Card" in response.text
    assert "View Card" not in response.text


def test_edit_page_shows_view_card_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-edit")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert 'href="https://api.scryfall.com/cards/sf-edit?format=image&version=large"' in response.text
    assert "View Card" in response.text


def test_batch_detail_shows_view_card_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-batch")
        batch_id = card.batch_id
    response = TestClient(main.app).get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert 'href="https://api.scryfall.com/cards/sf-batch?format=image&version=large"' in response.text
    assert "View Card" in response.text


def test_order_detail_shows_button_for_pre_allocation_order_item(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(external_order_id="o-1", source="manapool", status="needs_review")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name="Lightning Bolt", scryfall_id="sf-item",
            quantity=1,
        ))
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'href="https://api.scryfall.com/cards/sf-item?format=image&version=large"' in response.text


def test_order_detail_shows_button_for_allocated_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-allocated", status="reserved")
        order = SalesOrder(external_order_id="o-2", source="manapool", status="ready_to_pick")
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id, name="Forest", scryfall_id="sf-allocated", quantity=1,
        )
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id, batch_id=card.batch_id,
            status="allocated",
        ))
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert response.text.count(
        'href="https://api.scryfall.com/cards/sf-allocated?format=image&version=large"'
    ) == 2
