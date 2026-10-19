"""Two fixes that both stranded order 4731.

PART 1 -- UNDO EXCEPTION MARK put the line back to "allocated" always.
When a wave had already swept the order (Mark Wave Picked moved the
siblings to "picked" and DEFERRED this order because the exception needed
submission), that left the order permanently short of pick-complete: the
deferred promotion could never fire and the order sat in in_pick_wave with
no route forward on any screen. The line now rejoins its siblings.

PART 2 -- A WAVE ONLY CLOSED THE WAVE NAMED IN THE URL. An order can
belong to more than one wave -- membership is closed, not deleted -- so
order 4731 sat in wave 57 (swept it) and wave 59 (shipped it). Shipping
through wave 59 closed 59 and left 57 at "packed" with every one of its
shippable orders shipped, and nothing the operator could press.

Everything drives the real routes and the real services.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, Batch, FulfillmentException, InventoryCard, OrderItem, PickAllocation,
    PickWave, PickWaveOrder, SalesOrder,
)
from pick_wave_service import close_waves_holding_orders


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'undo.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "_push_processing_sync", lambda session, order: None)
    monkeypatch.setattr(main, "_push_shipment_sync", lambda session, order: None)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory", lambda **kw: [])
    return TestClient(main.app), engine


def make_order(session, batch, *, lines, status="ready_to_pick", label=None,
               alloc_status="allocated"):
    order = SalesOrder(
        external_order_id=f"u-{label or batch.id}-{status}-{lines}",
        external_label=label or f"70164{batch.id}{lines}", source="manapool",
        status=status, placed_at=datetime.now(), shipping_method="first_class",
        shipping_cents=0,
    )
    session.add(order)
    session.flush()
    allocations = []
    for index in range(lines):
        identity = {
            "name": f"Card {order.id}-{index}", "set_code": "soc",
            "collector_number": str(index), "scryfall_id": f"sf{order.id}-{index}",
            "mtgjson_id": f"mtg{order.id}-{index}", "language_id": "EN",
            "condition_id": "NM", "finish_id": "NF",
        }
        item = OrderItem(order_id=order.id, quantity=1, price_cents=250, **identity)
        session.add(item)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, condition="Near Mint", finish="normal",
            status="reserved", current_price=2.5, **identity,
        )
        session.add(card)
        session.flush()
        allocation = PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status=alloc_status,
        )
        session.add(allocation)
        session.flush()
        allocations.append(allocation.id)
    return order.id, allocations


def new_batch(session, code):
    batch = Batch(batch_code=code, is_archived=False)
    session.add(batch)
    session.flush()
    return batch


# --- PART 1: the reverted line rejoins its siblings ---------------------

def test_a_waved_order_promotes_itself_when_the_mark_is_undone(client):
    """★ THE ORDER 4731 CASE, end to end through the real routes."""
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A1")
        order_id, allocations = make_order(session, batch, lines=3)
        wave = PickWave(label="W", status="active", created_at=datetime.now())
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order_id, status="active"))
        order = session.get(SalesOrder, order_id)
        order.status = "in_pick_wave"
        # one line goes to exception, through the real service
        mark_fulfillment_exception(session, allocations[0], "missing",
                                   note="reported in test")
        session.commit()
        wave_id = wave.id
        exception_id = session.query(FulfillmentException).one().id

    # the wave is marked picked; this order defers
    http.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "in_pick_wave", "the order should have deferred"
        others = [session.get(PickAllocation, a).status for a in allocations[1:]]
        assert others == ["picked", "picked"], others

    # undo the mark
    response = http.post(f"/fulfillment-exceptions/{exception_id}/revert-mark",
                         data={"note": "marked by mistake"}, follow_redirects=False)
    assert response.status_code == 200, response.text[:300]

    with Session(engine) as session:
        restored = session.get(PickAllocation, allocations[0])
        assert restored.status == "picked", (
            "the line must rejoin its siblings, not drop back to allocated"
        )
        order = session.get(SalesOrder, order_id)
        assert order.status == "picked", (
            "and the order must finish the promotion the wave deferred"
        )


def test_the_order_page_then_offers_the_next_step(client):
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A2")
        order_id, allocations = make_order(session, batch, lines=2)
        wave = PickWave(label="W2", status="active", created_at=datetime.now())
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order_id, status="active"))
        session.get(SalesOrder, order_id).status = "in_pick_wave"
        mark_fulfillment_exception(session, allocations[0], "missing", note="x")
        session.commit()
        wave_id, exception_id = wave.id, session.query(FulfillmentException).one().id

    http.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    http.post(f"/fulfillment-exceptions/{exception_id}/revert-mark",
              data={"note": "mistake"}, follow_redirects=False)

    page = http.get(f"/orders/{order_id}").text
    assert "Mark Packed" in page or "packed" in page.lower(), (
        "a picked order's page should offer the next step"
    )


def test_an_unwaved_order_still_reverts_to_allocated(client):
    """Unchanged behaviour for the ordinary case."""
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A3")
        order_id, allocations = make_order(session, batch, lines=2)
        mark_fulfillment_exception(session, allocations[0], "missing", note="x")
        session.commit()
        exception_id = session.query(FulfillmentException).one().id

    http.post(f"/fulfillment-exceptions/{exception_id}/revert-mark",
              data={"note": "mistake"}, follow_redirects=False)

    with Session(engine) as session:
        assert session.get(PickAllocation, allocations[0]).status == "allocated"
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"


def test_a_waved_order_whose_siblings_are_not_picked_still_goes_to_allocated(client):
    """Only when the siblings really have moved on."""
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A4")
        order_id, allocations = make_order(session, batch, lines=3)
        wave = PickWave(label="W4", status="active", created_at=datetime.now())
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order_id, status="active"))
        session.get(SalesOrder, order_id).status = "in_pick_wave"
        mark_fulfillment_exception(session, allocations[0], "missing", note="x")
        session.commit()  # siblings still "allocated" -- no Mark Wave Picked
        exception_id = session.query(FulfillmentException).one().id

    http.post(f"/fulfillment-exceptions/{exception_id}/revert-mark",
              data={"note": "mistake"}, follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickAllocation, allocations[0]).status == "allocated"


def test_a_single_line_order_reverts_to_allocated(client):
    """No siblings to rejoin."""
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A5")
        order_id, allocations = make_order(session, batch, lines=1)
        mark_fulfillment_exception(session, allocations[0], "missing", note="x")
        session.commit()
        exception_id = session.query(FulfillmentException).one().id

    http.post(f"/fulfillment-exceptions/{exception_id}/revert-mark",
              data={"note": "mistake"}, follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickAllocation, allocations[0]).status == "allocated"


def test_the_confirm_no_longer_promises_the_prior_state(client):
    """The wording has to match what it now does."""
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "A6")
        order_id, allocations = make_order(session, batch, lines=2)
        mark_fulfillment_exception(session, allocations[0], "missing", note="x")
        session.commit()
    page = http.get(f"/orders/{order_id}").text
    if "Undo Exception Mark" in page:
        assert "restored to their prior state" not in page


# --- PART 2: a wave closes whichever route shipped its last order -------

def build_two_wave_order(session):
    """An order swept by one wave and shipped through another -- order
    4731's exact shape."""
    batch = new_batch(session, "B1")
    swept = PickWave(label="swept", status="packed", created_at=datetime.now())
    shipper = PickWave(label="shipper", status="packed", created_at=datetime.now())
    session.add_all([swept, shipper])
    session.flush()
    # the swept wave's other order already shipped
    done_id, done_allocs = make_order(session, batch, lines=1, status="shipped",
                                      label="111111-1111111", alloc_status="allocated")
    session.add(PickWaveOrder(wave_id=swept.id, order_id=done_id, status="active"))
    session.get(PickAllocation, done_allocs[0]).status = "shipped"
    session.get(InventoryCard, session.get(PickAllocation, done_allocs[0]).inventory_card_id).status = "sold"
    # the straggler, in BOTH waves
    order_id, allocations = make_order(session, batch, lines=1, status="packed",
                                       label="222222-2222222", alloc_status="packed")
    session.add(PickWaveOrder(wave_id=swept.id, order_id=order_id, status="closed"))
    session.add(PickWaveOrder(wave_id=shipper.id, order_id=order_id, status="active"))
    session.commit()
    return swept.id, shipper.id, order_id


def test_shipping_through_one_wave_closes_the_other_wave_too(client):
    """★ THE WAVE 57 BUG."""
    http, engine = client
    with Session(engine) as session:
        swept_id, shipper_id, order_id = build_two_wave_order(session)

    response = http.post(f"/pick-waves/{shipper_id}/ship",
                         data={"ship_order_ids": [order_id], "tracking_numbers": [""]},
                         follow_redirects=False)
    assert response.status_code == 200, response.text[:300]

    with Session(engine) as session:
        assert session.get(PickWave, shipper_id).status == "shipped"
        assert session.get(PickWave, swept_id).status == "shipped", (
            "the wave that swept the order must close too"
        )


def test_the_order_page_route_also_closes_both(client):
    http, engine = client
    with Session(engine) as session:
        swept_id, shipper_id, order_id = build_two_wave_order(session)

    http.post(f"/orders/{order_id}/shipped", data={"tracking_number": ""},
              follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickWave, swept_id).status == "shipped"
        assert session.get(PickWave, shipper_id).status == "shipped"


def test_a_wave_with_an_unshipped_order_is_left_alone(client):
    http, engine = client
    with Session(engine) as session:
        swept_id, shipper_id, order_id = build_two_wave_order(session)
        # give the swept wave something still outstanding
        batch = session.get(Batch, session.get(PickAllocation, 1).batch_id)
        extra_id, _ = make_order(session, batch, lines=1, status="packed",
                                 label="333333-3333333", alloc_status="packed")
        session.add(PickWaveOrder(wave_id=swept_id, order_id=extra_id, status="active"))
        session.commit()

    http.post(f"/pick-waves/{shipper_id}/ship",
              data={"ship_order_ids": [order_id], "tracking_numbers": [""]},
              follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickWave, swept_id).status == "packed", (
            "it still has an unshipped order"
        )


def test_the_closer_leaves_terminal_waves_alone(client):
    http, engine = client
    with Session(engine) as session:
        swept_id, shipper_id, order_id = build_two_wave_order(session)
        session.get(PickWave, swept_id).status = "cancelled"
        session.commit()

    with Session(engine) as session:
        moved = close_waves_holding_orders(session, [order_id])
        session.commit()
        assert swept_id not in [w.id for w in moved]
        assert session.get(PickWave, swept_id).status == "cancelled"


def test_the_closer_handles_an_order_in_no_wave(client):
    http, engine = client
    with Session(engine) as session:
        batch = new_batch(session, "C1")
        order_id, _ = make_order(session, batch, lines=1)
        session.commit()
    with Session(engine) as session:
        assert close_waves_holding_orders(session, [order_id]) == []
        assert close_waves_holding_orders(session, []) == []
        assert close_waves_holding_orders(session, [None]) == []
