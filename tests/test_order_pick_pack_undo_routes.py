from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import FulfillmentException, PickWave, PickWaveOrder, SalesOrder
from tests.test_order_detail_item13_redesign import make_order_with_allocation, setup_db


# CF-UNDO-002 item 1: unpick / unpack routes + order-detail wiring.

def test_order_detail_shows_unpick_button_when_not_wave_linked(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/unpick"' in response.text
    assert "Unpick" in response.text


def test_order_detail_hides_unpick_button_for_wave_linked_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        wave = PickWave(label="Wave 1", status="completed")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="closed"))
        session.commit()
        order_id, wave_id = order.id, wave.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/unpick"' not in response.text
    assert "Reopen Pick Wave from there" in response.text
    assert f'href="/pick-waves/{wave_id}"' in response.text


def test_unpick_route_success_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/unpick", follow_redirects=False)
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"


def test_unpick_route_refused_unless_picked(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/unpick")
    assert response.status_code == 409
    assert "Unpick Refused" in response.text


def test_order_detail_shows_unpack_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="packed", allocation_status="packed")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/unpack"' in response.text
    assert "Unpack" in response.text


def test_unpack_route_success_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="packed", allocation_status="packed")
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/unpack", follow_redirects=False)
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "picked"


def test_unpack_route_refused_unless_packed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id
    client = TestClient(main.app)
    response = client.post(f"/orders/{order_id}/unpack")
    assert response.status_code == 409
    assert "Unpack Refused" in response.text


def test_order_detail_still_shows_unpick_when_submission_block_hides_mark_packed(tmp_path, monkeypatch):
    # Unpick must stay available even when a pending fulfillment
    # exception blocks the FORWARD "Mark Packed" transition -- walking
    # back may be exactly what's needed to address that exception.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="picked", allocation_status="picked",
        )
        mark_fulfillment_exception(session, allocation.id, "missing", "found during pick")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "before marking this order packed" in response.text
    assert f'action="/orders/{order_id}/unpick"' in response.text


def test_order_detail_still_shows_unpack_when_submission_block_hides_ship(tmp_path, monkeypatch):
    # mark_fulfillment_exception() itself can never be called against an
    # already-packed allocation (ALLOWED_ALLOCATION_STATUSES excludes
    # "packed"), so this state is only reachable via direct reconciliation
    # writes, not the normal report-exception route -- constructed
    # directly here to exercise this page's own pure display branch.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="packed", allocation_status="packed",
        )
        session.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=item.id,
            pick_allocation_id=allocation.id, inventory_card_id=card.id,
            exception_type="missing", submission_state="needs_submission",
            note="found after packing",
        ))
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "before marking this order shipped" in response.text
    assert f'action="/orders/{order_id}/unpack"' in response.text
