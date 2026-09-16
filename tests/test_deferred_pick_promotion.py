"""An order blocked at wave completion must not be stranded for good.

complete_pick_wave sweeps every allocated line to "picked" but promotes
the ORDER only when nothing is awaiting Mana Pool submission. That is
correct: an unsubmitted exception means the customer's side has not been
told. The order stays "in_pick_wave" on purpose.

The gap was what came next. The wave went "completed" and the membership
"closed", and nothing re-evaluated the order. Submitting the exception
cleared the block, but complete_pick_wave only runs for an ACTIVE wave,
remove_order_from_wave and cancel_pick_wave both require one, and the Mark
Picked route only acts on "ready_to_pick". Order 4096 sat in
"in_pick_wave" belonging to no wave with no route forward on any screen.

The risk in fixing it is the opposite error: promoting an order whose pick
genuinely has not happened. Every test below that says "refuses" is
guarding that side.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, FulfillmentException, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)
from order_service import (
    order_is_pick_complete, promote_if_pick_complete,
    promote_stranded_pick_complete_orders,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'promote.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def stranded_order(session, *, wave_status="completed", membership="closed",
                   submit_exception=True, second_line_status="picked"):
    """Recreate order 4096: a completed wave left it behind because an
    exception was awaiting submission."""
    order, item, card, allocation = seed(session, order_status="in_pick_wave")
    exception = mark_fulfillment_exception(session, allocation.id, "missing")
    session.flush()

    # the other line, which the wave's sweep moved to picked
    second = OrderItem(
        order_id=order.id, name="Other", set_code="SET", collector_number="2",
        scryfall_id="sf2", mtgjson_id="mtg2", language_id="EN",
        condition_id="LP", finish_id="NF", quantity=1, price_cents=100,
    )
    session.add(second); session.flush()
    session.add(PickAllocation(
        order_item_id=second.id, inventory_card_id=card.id + 1000,
        batch_id=allocation.batch_id, status=second_line_status,
    ))

    wave = PickWave(label="Wave 1", status=wave_status)
    session.add(wave); session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status=membership))

    if submit_exception:
        exception.submission_state = "submitted"
    session.commit()
    return order, exception, wave


# --- the fix ------------------------------------------------------------

def test_a_stranded_order_is_promoted_once_its_exception_is_submitted(db):
    with Session(db) as session:
        order, _, _ = stranded_order(session)
        order_id = order.id
        assert promote_if_pick_complete(session, order) is True
        session.commit()

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "picked"
        assert order.picked_at is not None


def test_the_sweep_finds_it_without_being_told_which_order(db):
    with Session(db) as session:
        order, _, _ = stranded_order(session)
        order_id = order.id

    with Session(db) as session:
        moved = promote_stranded_pick_complete_orders(session)
        session.commit()
        assert [o.id for o in moved] == [order_id]

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "picked"


def test_submitting_the_exception_through_the_route_promotes_the_order(db):
    """The immediate path: submission is the moment the block clears."""
    with Session(db) as session:
        order, exception, _ = stranded_order(session, submit_exception=False)
        order_id, exception_id = order.id, exception.id

    TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "reported to Mana Pool"},
    )

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "picked"
        assert session.get(FulfillmentException, exception_id).submission_state == "submitted"


# --- the opposite error, which matters more -----------------------------

def test_it_refuses_while_a_live_wave_still_owns_the_order(db):
    """Promoting here would claim a pick that has not happened. The wave's
    own completion will handle it when the picker is actually done."""
    with Session(db) as session:
        order, _, _ = stranded_order(session, wave_status="active", membership="active")
        assert promote_if_pick_complete(session, order) is False
        session.commit()

    with Session(db) as session:
        assert session.query(SalesOrder).one().status == "in_pick_wave"


def test_it_refuses_while_the_exception_is_still_unsubmitted(db):
    """The original block is still correct: Mana Pool has not been told."""
    with Session(db) as session:
        order, _, _ = stranded_order(session, submit_exception=False)
        assert promote_if_pick_complete(session, order) is False


def test_it_refuses_when_a_line_was_never_picked(db):
    """An allocation still 'allocated' means the sweep never reached this
    order, so there is no completed pick to finish."""
    with Session(db) as session:
        order, _, _ = stranded_order(session, second_line_status="allocated")
        assert promote_if_pick_complete(session, order) is False


def test_an_order_with_no_allocations_is_not_pick_complete(db):
    """Vacuous truth is how a status gets invented: `all([])` is True."""
    with Session(db) as session:
        order = SalesOrder(external_order_id="empty", status="in_pick_wave", source="manapool")
        session.add(order); session.commit()
        assert order_is_pick_complete(session, order) is False
        assert promote_if_pick_complete(session, order) is False


def test_it_refuses_an_order_that_is_not_in_pick_wave(db):
    with Session(db) as session:
        order, _, _ = stranded_order(session)
        order.status = "shipped"
        session.commit()
        assert promote_if_pick_complete(session, order) is False


def test_the_sweep_leaves_healthy_orders_alone(db):
    with Session(db) as session:
        stranded_order(session, wave_status="active", membership="active")
        assert promote_stranded_pick_complete_orders(session) == []


def test_promotion_is_idempotent(db):
    with Session(db) as session:
        order, _, _ = stranded_order(session)
        assert promote_if_pick_complete(session, order) is True
        session.commit()
        # second call: already picked, nothing to do
        assert promote_if_pick_complete(session, order) is False
