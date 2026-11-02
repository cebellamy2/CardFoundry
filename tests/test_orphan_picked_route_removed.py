"""POST /orders/{id}/picked is gone, and nothing the operator sees moved.

The order-level Mark Picked route had no caller: Order Detail's
ready_to_pick branch renders no button for it, nothing in the app posted
to it, and the operator confirmed nothing outside the app does either.
Orders are marked picked by completing a pick wave. The route was removed
rather than kept "just in case" because a live write path nobody can see
is exactly the kind that gets reached by accident and then explained by
nobody.

The order is seeded in ready_to_pick -- the one status the old handler
acted on -- so a surviving route would have written, not refused.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, PickAllocation, SalesOrder
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'orphan_picked.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def a_ready_order(session):
    order, item, card, allocation = seed(session, order_status="ready_to_pick")
    order.status = "ready_to_pick"
    order.source = "manapool"
    session.commit()
    return order.id, allocation.id


def test_posting_to_the_removed_route_is_not_found_and_writes_nothing(db):
    with Session(db) as session:
        order_id, allocation_id = a_ready_order(session)
        allocation_status = session.get(PickAllocation, allocation_id).status

    response = TestClient(main.app).post(
        f"/orders/{order_id}/picked", follow_redirects=False,
    )
    assert response.status_code in (404, 405)

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"
        assert session.get(PickAllocation, allocation_id).status == allocation_status


def test_the_ready_to_pick_order_page_still_points_at_the_pick_wave(db):
    with Session(db) as session:
        order_id, _ = a_ready_order(session)

    page = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "waiting for the next master pick wave" in page
    assert f"/orders/{order_id}/picked" not in page
