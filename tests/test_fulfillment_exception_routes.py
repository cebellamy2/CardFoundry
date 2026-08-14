from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PickWave, PickWaveOrder
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'routes.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_order_detail_exposes_and_executes_shared_exception_service(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, allocation = seed(session)
        order_id, item_id, allocation_id = order.id, item.id, allocation.id
    client = TestClient(main.app)
    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert "Report Fulfillment Exception" in page.text
    response = client.post(
        f"/orders/{order_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "Not found"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        allocation = session.get(type(allocation), allocation_id)
        assert allocation.status == "exception"
        exception = session.query(main.FulfillmentException).one()
        assert exception.note == "Not found"
        confirmation = client.post(
            f"/fulfillment-exceptions/{exception.id}/submitted",
            data={"note": "Submitted to ManaPool"},
            follow_redirects=False,
    )
    assert confirmation.status_code == 303
    with Session(db) as session:
        assert session.get(main.FulfillmentException, exception.id).submission_state == "submitted"


def test_active_pick_wave_exposes_same_exception_action_and_preserves_siblings(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, allocation = seed(session)
        item_id = item.id
        wave = PickWave(label="route-wave", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id)); session.commit()
        wave_id, allocation_id = wave.id, allocation.id
    client = TestClient(main.app)
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert "Report Fulfillment Exception" in page.text
    response = client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "inventory_mismatch", "note": "Wrong printing"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        assert session.query(main.FulfillmentException).count() == 1
        assert session.get(type(allocation), allocation_id).status == "exception"
        assert session.get(type(item), item_id).quantity == 1
