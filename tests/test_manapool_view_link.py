from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation,
    PickWave, PickWaveOrder, RemoteProductBinding, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'manapool-view-link.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, name="Forest", scryfall_id="sf-1", status="available", batch_code=None):
    batch = Batch(batch_code=batch_code or f"B-{name}-{scryfall_id}")
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


def add_binding(session, card, *, set_code="fin", collector_number="42"):
    import json
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=f"product-{card.id}",
        local_card_ids_json=json.dumps([card.id]),
        requested_identity_json="{}",
        scryfall_id=card.scryfall_id or "sf-unknown", language_id="EN",
        condition_id="LP", finish_id="NF", set_code=set_code, collector_number=collector_number,
        binding_status="validated", validated_at=datetime.now(),
        evidence_hash=f"evidence-{card.id}", evidence_json="{}",
    )
    session.add(binding)
    session.commit()
    return binding


# -- unit tests -------------------------------------------------------------

def test_product_url_builds_from_set_and_collector_number():
    assert main._manapool_product_url("FIN", "42") == "https://manapool.com/card/fin/42"


def test_product_url_lowercases_and_strips():
    assert main._manapool_product_url(" FIN ", " 42 ") == "https://manapool.com/card/fin/42"


def test_product_url_none_when_set_code_missing():
    assert main._manapool_product_url(None, "42") is None
    assert main._manapool_product_url("", "42") is None


def test_product_url_none_when_collector_number_missing():
    assert main._manapool_product_url("fin", None) is None
    assert main._manapool_product_url("fin", "") is None


def test_view_link_renders_a_button_linked_to_product_page():
    html = main._manapool_view_link("fin", "42")
    assert html == (
        '<a href="https://manapool.com/card/fin/42" target="_blank" rel="noopener" '
        'class="manapool-view-link">View on Mana Pool</a>'
    )


def test_view_link_is_empty_when_set_or_collector_number_missing():
    assert main._manapool_view_link(None, "42") == ""
    assert main._manapool_view_link("fin", None) == ""


def test_bindings_by_card_id_empty_for_no_ids():
    assert main._manapool_bindings_by_card_id(None, []) == {}


def test_bindings_by_card_id_maps_only_requested_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        bound = add_card(session, name="Bound", scryfall_id="sf-bound")
        unbound = add_card(session, name="Unbound", scryfall_id="sf-unbound")
        add_binding(session, bound)
        result = main._manapool_bindings_by_card_id(session, [bound.id, unbound.id])
        assert set(result.keys()) == {bound.id}
        assert result[bound.id].set_code == "fin"


def test_view_link_for_card_empty_when_no_binding():
    assert main._manapool_view_link_for_card({}, 1) == ""
    assert main._manapool_view_link_for_card({}, None) == ""


def test_view_link_for_card_renders_from_binding(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session)
        binding = add_binding(session, card, set_code="woe", collector_number="7")
        html = main._manapool_view_link_for_card({card.id: binding}, card.id)
        assert 'href="https://manapool.com/card/woe/7"' in html
        assert "View on Mana Pool" in html


# -- route tests --------------------------------------------------------

def test_inventory_search_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, name="Bolt", scryfall_id="sf-bolt")
        add_binding(session, card, set_code="lea", collector_number="161")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/lea/161"' in response.text
    assert "View on Mana Pool" in response.text


def test_inventory_search_hides_manapool_button_when_unbound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Unlisted Card", scryfall_id="sf-unlisted")
    response = TestClient(main.app).get("/inventory?show_all=true")
    assert response.status_code == 200
    assert "Unlisted Card" in response.text
    assert "View on Mana Pool" not in response.text


def test_edit_page_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-edit")
        add_binding(session, card, set_code="m10", collector_number="99")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/m10/99"' in response.text


def test_edit_page_hides_manapool_button_when_unbound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-edit-2")
        card_id = card.id
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert "View on Mana Pool" not in response.text


def test_batch_detail_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-batch")
        add_binding(session, card, set_code="grn", collector_number="5")
        batch_id = card.batch_id
    response = TestClient(main.app).get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/grn/5"' in response.text


def test_batch_detail_hides_manapool_button_when_unbound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-batch-2")
        batch_id = card.batch_id
    response = TestClient(main.app).get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert "View on Mana Pool" not in response.text


def test_order_detail_pre_allocation_item_uses_its_own_set_and_collector_number(tmp_path, monkeypatch):
    """OrderItem is inherently already a Mana Pool transaction -- it carries
    its own set_code/collector_number directly, no binding lookup needed."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(external_order_id="o-1", source="manapool", status="needs_review")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name="Lightning Bolt", scryfall_id="sf-item",
            set_code="lea", collector_number="161", quantity=1,
        ))
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/lea/161"' in response.text


def test_order_detail_pre_allocation_item_hides_button_when_no_set_or_collector_number(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(external_order_id="o-2", source="manapool", status="needs_review")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name="Mystery Item", scryfall_id=None, quantity=1,
        ))
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "View on Mana Pool" not in response.text


def test_order_detail_allocated_card_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-allocated", status="reserved")
        add_binding(session, card, set_code="dom", collector_number="3")
        order = SalesOrder(external_order_id="o-3", source="manapool", status="ready_to_pick")
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name="Forest", scryfall_id="sf-allocated", quantity=1)
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
    assert 'href="https://manapool.com/card/dom/3"' in response.text


def make_wave_with_card(session, *, bound, batch_code="B1"):
    order = SalesOrder(
        external_order_id=f"order-{session.query(SalesOrder).count() + 1}",
        status="in_pick_wave",
    )
    session.add(order)
    session.flush()
    batch = Batch(batch_code=batch_code)
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", scryfall_id="sf-alpha", mtgjson_id="MTG-ALPHA",
        set_code="LEA", collector_number="1",
        language_id="EN", condition_id="LP", finish_id="NF", finish="normal", status="reserved",
    )
    session.add(card)
    session.flush()
    if bound:
        add_binding(session, card, set_code="lea", collector_number="1")
    item = OrderItem(
        order_id=order.id, name="Alpha", scryfall_id="sf-alpha", mtgjson_id="MTG-ALPHA",
        set_code="LEA", collector_number="1",
        language_id="EN", condition_id="LP", finish_id="NF", quantity=1,
    )
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id, status="allocated",
    ))
    wave = PickWave(label="Wave", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.flush()
    return wave, order, item, card


def test_pick_wave_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, _, _, _ = make_wave_with_card(session, bound=True)
        session.commit()
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/lea/1"' in response.text


def test_pick_wave_hides_manapool_button_when_unbound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, _, _, _ = make_wave_with_card(session, bound=False)
        session.commit()
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "View on Mana Pool" not in response.text


def test_pick_wave_fulfillment_exception_table_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order, item, card = make_wave_with_card(session, bound=True)
        allocation = session.query(PickAllocation).filter_by(order_item_id=item.id).one()
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "Fulfillment Exceptions" in response.text
    assert 'href="https://manapool.com/card/lea/1"' in response.text


def test_order_detail_fulfillment_exception_table_shows_manapool_button_when_bound(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_card(session, scryfall_id="sf-exc", status="reserved")
        card.mtgjson_id = "MTG-EXC"
        card.set_code = "ISD"
        card.collector_number = "88"
        session.commit()
        add_binding(session, card, set_code="isd", collector_number="88")
        order = SalesOrder(external_order_id="o-4", source="manapool", status="ready_to_pick")
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id, name="Forest", scryfall_id="sf-exc", mtgjson_id="MTG-EXC",
            set_code="ISD", collector_number="88",
            language_id="EN", condition_id="LP", finish_id="NF", quantity=1,
        )
        session.add(item)
        session.flush()
        allocation = PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id, batch_id=card.batch_id,
            status="allocated",
        )
        session.add(allocation)
        session.flush()
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'href="https://manapool.com/card/isd/88"' in response.text
