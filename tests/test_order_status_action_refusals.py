"""A status action that cannot apply must say so, never no-op silently.

Found during v1.166.0: Mark Picked only acted on a ready_to_pick order and
on anything else fell straight through to a redirect. The operator landed
back on the order looking unchanged, with no way to tell a successful
write from one that never happened -- which is how order 4096 looked
merely "stuck" for two days, and the same shape as the old Resolve bug
that reported success while writing nothing.

Answered with a refusal PAGE rather than a new flash channel: this
codebase has no post-redirect message mechanism, and three neighbours in
the same area (Unpick, Unpack, Uncancel) already answer refusals this way.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, SalesOrder
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'refusals.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def an_order(session, status):
    order, item, card, allocation = seed(session, order_status=status)
    order.status = status
    order.source = "manapool"
    session.commit()
    return order


# route, the status it needs, a status it must refuse, the action name
CASES = [
    ("picked", "ready_to_pick", "shipped", "Mark Picked"),
    ("packed", "picked", "ready_to_pick", "Mark Packed"),
    ("shipped", "packed", "ready_to_pick", "Mark Shipped"),
    ("cancel", "ready_to_pick", "shipped", "Cancel"),
    ("approve", "short", "shipped", "Retry Allocation"),
]


@pytest.mark.parametrize("path,applicable,wrong,action", CASES)
def test_the_action_refuses_in_plain_words_instead_of_no_opping(
    db, path, applicable, wrong, action,
):
    with Session(db) as session:
        order = an_order(session, wrong)
        order_id = order.id

    response = TestClient(main.app).post(f"/orders/{order_id}/{path}")
    assert response.status_code == 409
    assert action in response.text
    # names the current status AND what would make it applicable
    assert wrong.replace("_", " ") in response.text
    assert "nothing was changed" in response.text


@pytest.mark.parametrize("path,applicable,wrong,action", CASES)
def test_the_refusal_does_not_change_the_order(db, path, applicable, wrong, action):
    with Session(db) as session:
        order = an_order(session, wrong)
        order_id = order.id

    TestClient(main.app).post(f"/orders/{order_id}/{path}")

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == wrong


def test_mark_shipped_refusal_does_not_swallow_the_typed_tracking_number(db):
    """The worst of the silent no-ops: the operator typed a tracking
    number and a plain redirect discarded it without a word."""
    with Session(db) as session:
        order = an_order(session, "ready_to_pick")
        order_id = order.id

    response = TestClient(main.app).post(
        f"/orders/{order_id}/shipped", data={"tracking_number": "1Z-TEST-999"},
    )
    assert response.status_code == 409
    assert "Mark Shipped" in response.text

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "ready_to_pick"
        assert order.tracking_number is None


def test_a_missing_order_is_named_rather_than_silently_redirected(db):
    response = TestClient(main.app).post("/orders/999999/packed")
    assert response.status_code == 409
    assert "no longer exists" in response.text


@pytest.mark.parametrize("path,action", [
    ("retry-shipment-sync", "Retry shipment sync"),
    ("retry-processing-sync", "Retry processing sync"),
])
def test_the_retry_sync_routes_refuse_in_plain_words(db, path, action):
    """These two are the ones that failed silently in ORDINARY use: their
    guards include remote-sync timestamps that change underneath the
    operator, not just an order status."""
    with Session(db) as session:
        order = an_order(session, "ready_to_pick")
        order_id = order.id

    response = TestClient(main.app).post(f"/orders/{order_id}/{path}")
    assert response.status_code == 409
    assert action in response.text


def test_a_successful_action_still_works(db):
    with Session(db) as session:
        order = an_order(session, "ready_to_pick")
        order_id = order.id

    response = TestClient(main.app).post(
        f"/orders/{order_id}/cancel", data={"cancel_reason": "buyer_requested"},
    )
    assert response.status_code in (200, 303)

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"


def test_every_status_action_has_a_stated_requirement():
    """The refusal text is only useful if it can name what WOULD work."""
    for action, requirement in main.ORDER_STATUS_ACTION_REQUIREMENTS.items():
        assert requirement and requirement[0].islower(), action
