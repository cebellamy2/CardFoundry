"""Cancel a SHIPPED order the carrier never collected, and restock it.

THE SITUATION (operator, 2026-10-19): an order is marked shipped here, the
carrier never takes the package, and the buyer cancels. Until now there
was NO path from shipped/sold back to available -- release_order needs a
reserved card, change_sellability needs an unsellable one, un_remove needs
a removed one, and nothing anywhere cleared sold_price or the consignment
amount that shipping froze onto the card.

★ IT IS A MODE OF release_order, NOT A SECOND PATH. The audit row, the
exception settling, the pick-wave detach and the undo are the ones that
already existed. What it adds is: collect the shipped allocation, take the
sold card back, and undo what shipping put on it.

★ AND IT IS ONLY OFFERED WHILE THE PACKAGE IS STILL HERE -- within 72
hours, no tracking number, every card untouched since shipping, and no
consignment payout already paid. Each of those is tested from both ends:
the button is not drawn, AND the route refuses.

FIXTURES GO THROUGH THE REAL SERVICES -- mark_shipped puts the order into
the state under test, so the state cannot drift from what production has.
MANA POOL IS NEVER CONTACTED: the fixture replaces its HTTP client so any
attempt fails the test loudly.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import manapool_service
import order_service
from models import (
    Base, Batch, Consignor, InventoryCard, InventoryChangeLog, OrderCancellation,
    OrderItem, PickAllocation, SalesOrder,
)
from order_service import (
    NOT_COLLECTED_WINDOW_HOURS, mark_shipped, not_collected_refusal,
    release_order, uncancel_order,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'notcollected.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])

    class NoManaPool:
        def Client(self, *a, **k):
            raise AssertionError("this action must never contact Mana Pool")

    monkeypatch.setattr(manapool_service, "httpx", NoManaPool())
    return TestClient(main.app), engine


def build_shipped(session, *, consigned=False, paid=False, cards=1,
                  tracking=None, price_cents=250):
    """An order put into the shipped state by the REAL mark_shipped."""
    consignor = None
    if consigned:
        consignor = Consignor(name="Jane Doe", payout_method="Cash App: @x",
                              is_active=True)
        session.add(consignor)
        session.flush()
    batch = Batch(batch_code="A1", is_archived=False, is_consignment=consigned,
                  consignor_id=consignor.id if consignor else None)
    session.add(batch)
    session.flush()
    order = SalesOrder(
        external_order_id="u-1", external_label="701117-2470451",
        source="manapool", status="packed", placed_at=datetime.now(),
        shipping_method="first_class", shipping_cents=0,
    )
    session.add(order)
    session.flush()
    made = []
    for index in range(cards):
        identity = {
            "name": f"Skycat Sovereign {index}", "set_code": "soc",
            "collector_number": str(index), "scryfall_id": f"sf{index}",
            "mtgjson_id": f"mtg{index}", "language_id": "EN",
            "condition_id": "NM", "finish_id": "NF",
        }
        item = OrderItem(order_id=order.id, quantity=1,
                         price_cents=price_cents, **identity)
        session.add(item)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, condition="Near Mint", finish="normal",
            status="reserved", current_price=2.5, **identity,
        )
        session.add(card)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status="packed",
        ))
        made.append(card.id)
    session.flush()

    mark_shipped(session, order, tracking)
    session.flush()
    if paid:
        for card_id in made:
            session.get(InventoryCard, card_id).consignment_payout_status = "paid"
    session.commit()
    return order.id, made


def age_the_order(session, order_id, hours):
    order = session.get(SalesOrder, order_id)
    order.shipped_at = datetime.now() - timedelta(hours=hours)
    session.commit()


def order_page(http, order_id):
    return http.get(f"/orders/{order_id}").text


ACTION = "Not collected"


# --- the state mark_shipped really leaves ------------------------------

def test_the_fixture_really_is_shipped_and_sold(client):
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
    with Session(engine) as session:
        assert session.get(SalesOrder, order_id).status == "shipped"
        card = session.get(InventoryCard, cards[0])
        assert card.status == "sold" and card.sold_price == 2.50


# --- the button, per guard ---------------------------------------------

def test_the_action_is_offered_on_a_freshly_shipped_order(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
    assert ACTION in order_page(http, order_id)


def test_it_is_hidden_past_the_window(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
        age_the_order(session, order_id, NOT_COLLECTED_WINDOW_HOURS + 1)
    assert ACTION not in order_page(http, order_id)


def test_it_is_still_offered_just_inside_the_window(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
        age_the_order(session, order_id, NOT_COLLECTED_WINDOW_HOURS - 1)
    assert ACTION in order_page(http, order_id)


def test_it_is_hidden_when_the_order_has_tracking(client):
    """A tracking number means the label was used."""
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session, tracking="9400111899223")
    assert ACTION not in order_page(http, order_id)


def test_it_is_hidden_once_a_card_has_moved(client):
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
        session.get(InventoryCard, cards[0]).status = "removed"
        session.commit()
    assert ACTION not in order_page(http, order_id)


def test_it_is_hidden_when_a_consignor_has_already_been_paid(client):
    """★ A PAID PAYOUT CANNOT BE REVERSED, so the whole action refuses."""
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session, consigned=True, paid=True)
    assert ACTION not in order_page(http, order_id)


def test_it_is_not_offered_on_an_unshipped_order(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
        order = session.get(SalesOrder, order_id)
        order.status = "packed"
        session.commit()
    assert ACTION not in order_page(http, order_id)


# --- the route re-checks, it does not trust the page -------------------

@pytest.mark.parametrize("break_it,expected", [
    ("window", "window"),
    ("tracking", "tracking number"),
    ("moved", "moved on"),
    ("paid", "already been paid"),
])
def test_the_route_refuses_even_when_the_button_was_stale(client, break_it, expected):
    """★ A TAB LEFT OPEN MUST NOT BE ABLE TO UN-SELL STOCK."""
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session, consigned=(break_it == "paid"))
        if break_it == "window":
            age_the_order(session, order_id, NOT_COLLECTED_WINDOW_HOURS + 2)
        elif break_it == "tracking":
            session.get(SalesOrder, order_id).tracking_number = "9400111899223"
            session.commit()
        elif break_it == "moved":
            session.get(InventoryCard, cards[0]).status = "available"
            session.commit()
        elif break_it == "paid":
            session.get(InventoryCard, cards[0]).consignment_payout_status = "paid"
            session.commit()

    response = http.post(f"/orders/{order_id}/not-collected",
                         data={"cancel_note": ""}, follow_redirects=False)
    assert response.status_code != 303
    assert expected in response.text

    with Session(engine) as session:
        assert session.get(SalesOrder, order_id).status == "shipped", (
            "nothing may change when the action is refused"
        )


# --- what it actually does ---------------------------------------------

def test_the_cards_come_back_into_stock(client):
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session, cards=2)

    response = http.post(f"/orders/{order_id}/not-collected",
                         data={"cancel_note": "carrier never came"},
                         follow_redirects=False)
    assert response.status_code == 303, response.text[:300]

    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "cancelled"
        assert order.cancelled_from_status == "shipped"
        for card_id in cards:
            card = session.get(InventoryCard, card_id)
            assert card.status == "available"
            assert card.sold_price is None, "there was no sale"
        allocations = session.query(PickAllocation).all()
        assert all(a.status == "released" for a in allocations)
        assert all(a.released_from_status == "shipped" for a in allocations)


def test_it_writes_a_cancellation_row_and_a_card_audit_row(client):
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
    http.post(f"/orders/{order_id}/not-collected",
              data={"cancel_note": "note"}, follow_redirects=False)

    with Session(engine) as session:
        row = session.query(OrderCancellation).filter(
            OrderCancellation.sales_order_id == order_id).one()
        assert row.reason == "not_collected"
        assert row.previous_order_status == "shipped"
        assert row.released_card_count == 1
        logs = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == cards[0]).all()
        kinds = [json.loads(l.change_summary)["action_type"] for l in logs]
        assert "not_collected_restock" in kinds, (
            "mark_shipped writes no card audit row, so this one must"
        )


def test_a_consigned_card_stops_owing_money(client):
    """★ THE MONEY. Shipping froze an amount owed onto the card; there was
    no sale, so it must not survive the reversal."""
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session, consigned=True)
        card = session.get(InventoryCard, cards[0])
        assert card.consignment_payout_status == "owed"
        owed_before = card.consignment_amount_owed

    http.post(f"/orders/{order_id}/not-collected", data={"cancel_note": ""},
              follow_redirects=False)

    with Session(engine) as session:
        card = session.get(InventoryCard, cards[0])
        assert card.consignment_payout_status is None
        assert card.consignment_amount_owed is None
    assert owed_before is not None, "the fixture should have owed something"


def test_an_ordinary_cancellation_still_cannot_touch_a_shipped_order(client):
    """★ THE MODE IS OPT-IN. Without it, release_order must not collect a
    shipped allocation -- those cards are with a customer."""
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
        order = session.get(SalesOrder, order_id)
        release_order(session, order, reason="other", note="ordinary")
        session.commit()

    with Session(engine) as session:
        card = session.get(InventoryCard, cards[0])
        assert card.status == "sold", "an ordinary cancel must not restock it"
        assert card.sold_price is not None


def test_the_ordinary_cancel_button_is_not_offered_on_a_shipped_order(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
    assert "Cancel &amp; Release Cards" not in order_page(http, order_id)


# --- the undo ------------------------------------------------------------

def test_undo_restores_everything_exactly(client):
    """★ SNAPSHOT-BASED. The card goes back to sold WITH its price and its
    consignment amount, the allocation back to shipped, the order back to
    shipped."""
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session, consigned=True)
        card = session.get(InventoryCard, cards[0])
        before = (card.status, card.sold_price, card.consignment_amount_owed,
                  card.consignment_payout_status)
        shipped_at_before = session.get(SalesOrder, order_id).shipped_at

    http.post(f"/orders/{order_id}/not-collected", data={"cancel_note": ""},
              follow_redirects=False)

    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        uncancel_order(session, order)
        session.commit()

    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        card = session.get(InventoryCard, cards[0])
        after = (card.status, card.sold_price, card.consignment_amount_owed,
                 card.consignment_payout_status)
        assert after == before, f"{before} -> {after}"
        assert order.status == "shipped"
        assert order.shipped_at == shipped_at_before
        allocation = session.query(PickAllocation).one()
        assert allocation.status == "shipped"
        assert allocation.released_from_status is None


def test_undo_refuses_when_a_card_has_moved_on(client):
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
    http.post(f"/orders/{order_id}/not-collected", data={"cancel_note": ""},
              follow_redirects=False)

    with Session(engine) as session:
        session.get(InventoryCard, cards[0]).status = "removed"
        session.commit()

    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        with pytest.raises(order_service.InventoryAllocationError):
            uncancel_order(session, order)


def test_an_ordinary_cancellation_still_undoes_to_reserved(client):
    """The pre-existing behaviour must be untouched by the new snapshot."""
    http, engine = client
    with Session(engine) as session:
        batch = Batch(batch_code="B1", is_archived=False)
        session.add(batch)
        session.flush()
        order = SalesOrder(external_order_id="u-2", external_label="999999-9999999",
                           source="manapool", status="picked",
                           placed_at=datetime.now(), shipping_cents=0)
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, quantity=1, name="C", price_cents=100)
        session.add(item)
        session.flush()
        card = InventoryCard(batch_id=batch.id, name="C", condition="Near Mint",
                             finish="normal", status="reserved", current_price=1.0)
        session.add(card)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                                   batch_id=batch.id, status="picked"))
        session.commit()
        order_id, card_id = order.id, card.id

    with Session(engine) as session:
        release_order(session, session.get(SalesOrder, order_id), reason="other")
        session.commit()
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).status == "available"
        uncancel_order(session, session.get(SalesOrder, order_id))
        session.commit()
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        assert card.status == "reserved", "unchanged behaviour"
        assert session.query(PickAllocation).one().status == "picked"


# --- the refusal helper itself ------------------------------------------

def test_the_refusal_helper_passes_a_good_order(client):
    http, engine = client
    with Session(engine) as session:
        order_id, _ = build_shipped(session)
        assert not_collected_refusal(session, session.get(SalesOrder, order_id)) is None


def test_mana_pool_is_never_contacted(client):
    """The fixture makes any Mana Pool call raise, so this passing is it."""
    http, engine = client
    with Session(engine) as session:
        order_id, cards = build_shipped(session)
    assert http.post(f"/orders/{order_id}/not-collected", data={"cancel_note": ""},
                     follow_redirects=False).status_code == 303
    with Session(engine) as session:
        assert session.get(InventoryCard, cards[0]).status == "available"
