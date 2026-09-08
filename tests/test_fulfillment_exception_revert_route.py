from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, FulfillmentException, InventoryCard, PickAllocation, SalesOrder
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'revert-route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_marked_exception(session, kind="missing"):
    order, item, card, allocation = seed(session)
    exception = mark_fulfillment_exception(session, allocation.id, kind, "found during pick")
    session.commit()
    return order, item, card, allocation, exception


def test_order_page_shows_undo_button_for_needs_submission_unresolved_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _, _, _, exception = make_marked_exception(session)
        exception_id, order_id = exception.id, order.id

    client = TestClient(main.app)
    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert f'action="/fulfillment-exceptions/{exception_id}/revert-mark"' in page.text
    assert "Undo Exception Mark" in page.text


def test_pick_wave_detail_shows_undo_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation, exception = make_marked_exception(session)
        from models import PickWave, PickWaveOrder
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()
        wave_id, exception_id = wave.id, exception.id

    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert f'action="/fulfillment-exceptions/{exception_id}/revert-mark"' in page.text


def test_revert_route_success_restores_card_allocation_and_redirects_display(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation, exception = make_marked_exception(session)
        order_id, exception_id, card_id, allocation_id = order.id, exception.id, card.id, allocation.id

    client = TestClient(main.app)
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "Operator mis-clicked; card was on the shelf."},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Fulfillment Exception Mark Reverted" in response.text
    assert f'href="/orders/{order_id}"' in response.text

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        allocation = session.get(PickAllocation, allocation_id)
        exception = session.get(FulfillmentException, exception_id)
        assert card.status == "reserved"
        assert allocation.status == "allocated"
        assert exception.inventory_resolution_state == "resolved"
        assert exception.submission_state == "not_required"


def test_revert_route_refused_once_submitted_to_manapool(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation, exception = make_marked_exception(session)
        exception.submission_state = "submitted"
        session.commit()
        exception_id, card_id = exception.id, card.id

    client = TestClient(main.app)
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "Undo reason"},
    )
    assert response.status_code == 409
    assert "Undo Refused" in response.text

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "removed"


def test_revert_route_refused_once_order_progressed_past_undoable_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation, exception = make_marked_exception(session, kind="inventory_mismatch")
        order.status = "shipped"
        session.commit()
        exception_id, card_id = exception.id, card.id

    client = TestClient(main.app)
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "Undo reason"},
    )
    assert response.status_code == 409
    assert "Undo Refused" in response.text

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "unsellable"


def test_revert_route_404_for_missing_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/fulfillment-exceptions/999/revert-mark", data={"note": "Undo reason"},
    )
    assert response.status_code == 404
