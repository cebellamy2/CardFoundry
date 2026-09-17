"""A cancelled or all-exception order must never hold up a pick wave.

Reported live on 2026-09-17 against wave 40: 29 packed orders could not be
shipped, and the cancelled order in the wave was blamed. It was not the
cause -- see test_a_cancelled_order_never_reaches_the_ship_gate, which
pins the property that was already true -- but the wave did contain an
order nothing could move: order 4138, "picked", every line at
"exception", remote status "replaced". That one is real, and these tests
hold both halves so neither can regress into the other's story.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
import order_service
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    import inventory_sync_service
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def make_order(session, *, label, status, shipping_method="first_class",
               allocation_statuses=("picked",), wave=None,
               membership_status="active"):
    """One order with one allocation per entry in allocation_statuses."""
    batch = session.query(Batch).first()
    if not batch:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
    order = SalesOrder(
        external_order_id=f"ext-{label}", external_label=label,
        source="manapool", status=status, shipping_method=shipping_method,
    )
    session.add(order)
    session.flush()
    for i, alloc_status in enumerate(allocation_statuses):
        card = InventoryCard(
            batch_id=batch.id, name=f"Card {label}-{i}", status="reserved",
            mtgjson_id=f"m{label}{i}", language_id="EN",
            condition_id="NM", finish_id="NF",
        )
        session.add(card)
        session.flush()
        item = OrderItem(
            order_id=order.id, name=card.name, quantity=1, price_cents=100,
            mtgjson_id=card.mtgjson_id, language_id="EN",
            condition_id="NM", finish_id="NF",
        )
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status=alloc_status,
        ))
    if wave is not None:
        session.add(PickWaveOrder(
            wave_id=wave.id, order_id=order.id, status=membership_status,
        ))
    session.flush()
    return order


@pytest.fixture
def wave(db):
    with Session(db) as session:
        row = PickWave(label="Wave under test", status="completed")
        session.add(row)
        session.commit()
        return row.id


# --- the predicate ---------------------------------------------------------

def test_an_order_whose_every_line_is_an_exception_has_nothing_to_ship(db):
    with Session(db) as session:
        order = make_order(session, label="all-exc", status="picked",
                           allocation_statuses=("exception",))
        session.commit()
        assert order_service.order_has_nothing_to_ship(session, order) is True


def test_one_shippable_line_is_enough_to_gate_normally(db):
    """The opposite error: a part-exception order still owes the customer
    a package, and must keep demanding a tracking number."""
    with Session(db) as session:
        order = make_order(session, label="mixed", status="packed",
                           allocation_statuses=("exception", "packed"))
        session.commit()
        assert order_service.order_has_nothing_to_ship(session, order) is False


def test_an_order_with_no_allocations_at_all_is_not_nothing_to_ship(db):
    """Vacuous truth is how a status gets invented. A wave never picked
    this order."""
    with Session(db) as session:
        order = SalesOrder(external_order_id="bare", external_label="bare",
                           source="manapool", status="packed")
        session.add(order)
        session.commit()
        assert order_service.order_has_nothing_to_ship(session, order) is False


def test_a_cancelled_order_is_not_reported_as_nothing_to_ship(db):
    """Different state, different audit row. Callers check it separately
    and the wave row says "Cancelled", not "nothing to ship"."""
    with Session(db) as session:
        order = make_order(session, label="canx", status="cancelled",
                           allocation_statuses=("exception",))
        session.commit()
        assert order_service.orders_with_nothing_to_ship(session, [order]) == set()


# --- the ship gate ---------------------------------------------------------

def test_a_cancelled_order_never_reaches_the_ship_gate(db, wave):
    """The property the wave-40 report was about. It already held -- the
    gate filters on status == "packed" -- and this pins it so a future
    widening of that filter cannot lose it silently."""
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="ok-1", status="packed",
                   shipping_method="first_class",
                   allocation_statuses=("packed",), wave=wave_row,
                   membership_status="closed")
        make_order(session, label="canx", status="cancelled",
                   shipping_method="ground_advantage",
                   allocation_statuses=("exception",), wave=wave_row,
                   membership_status="closed")
        session.commit()

    response = TestClient(main.app).post(
        f"/pick-waves/{wave}/ship", data={"ship_order_ids": [], "tracking_numbers": []},
    )
    assert response.status_code == 200, (
        "the one packed first_class order ships; the cancelled "
        "ground_advantage order must not demand tracking"
    )
    assert "Tracking numbers required" not in response.text

    with Session(db) as session:
        shipped = session.query(SalesOrder).filter_by(external_label="ok-1").one()
        assert shipped.status == "shipped"
        cancelled = session.query(SalesOrder).filter_by(external_label="canx").one()
        assert cancelled.status == "cancelled", "untouched"


def test_a_nothing_to_ship_order_never_reaches_the_ship_gate(db, wave):
    """Order 4138's shape. Even at "packed" it has no card to send."""
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="ok-2", status="packed",
                   allocation_statuses=("packed",), wave=wave_row,
                   membership_status="closed")
        make_order(session, label="empty", status="packed",
                   shipping_method="ground_advantage",
                   allocation_statuses=("exception",), wave=wave_row,
                   membership_status="closed")
        session.commit()

    response = TestClient(main.app).post(f"/pick-waves/{wave}/ship", data={})
    assert response.status_code == 200
    assert "Tracking numbers required" not in response.text

    with Session(db) as session:
        assert session.query(SalesOrder).filter_by(external_label="ok-2").one().status == "shipped"
        assert session.query(SalesOrder).filter_by(external_label="empty").one().status == "packed", (
            "not shipped -- there was nothing to ship"
        )


def test_a_part_exception_order_still_demands_its_tracking_number(db, wave):
    """The guard against over-correcting: one real line means one real
    package."""
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="part", status="packed",
                   shipping_method="ground_advantage",
                   allocation_statuses=("exception", "packed"), wave=wave_row,
                   membership_status="closed")
        session.commit()

    response = TestClient(main.app).post(f"/pick-waves/{wave}/ship", data={})
    assert response.status_code == 400
    assert "Tracking numbers required" in response.text
    assert "part" in response.text


# --- the wave page ---------------------------------------------------------

def test_the_wave_page_explains_both_non_shipping_rows_in_plain_words(db, wave):
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="canx", status="cancelled",
                   allocation_statuses=("exception",), wave=wave_row,
                   membership_status="closed")
        make_order(session, label="empty", status="picked",
                   allocation_statuses=("exception",), wave=wave_row,
                   membership_status="closed")
        session.commit()

    text = TestClient(main.app).get(f"/pick-waves/{wave}").text
    assert "Cancelled &mdash; nothing to ship" in text
    assert "every line is a fulfillment exception" in text


def test_neither_row_is_highlighted_as_tracking_required(db, wave):
    """Keyed on the shipping method alone, the red "tracking required"
    highlight fired on a cancelled row too -- which is how a cancelled
    order came to be read as the thing blocking a wave."""
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="canx", status="cancelled",
                   shipping_method="ground_advantage",
                   allocation_statuses=("exception",), wave=wave_row,
                   membership_status="closed")
        session.commit()

    text = TestClient(main.app).get(f"/pick-waves/{wave}").text
    assert 'class="tracking-required"' not in text


def test_a_genuinely_packed_row_is_still_highlighted(db, wave):
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        make_order(session, label="real", status="packed",
                   shipping_method="ground_advantage",
                   allocation_statuses=("packed",), wave=wave_row,
                   membership_status="closed")
        session.commit()

    text = TestClient(main.app).get(f"/pick-waves/{wave}").text
    assert 'class="tracking-required"' in text


# --- membership closes on cancel, from BOTH surfaces -----------------------

def test_the_manual_cancel_route_closes_wave_membership(db, wave):
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        order = make_order(session, label="m-cancel", status="packed",
                           allocation_statuses=("packed",), wave=wave_row)
        session.commit()
        order_id = order.id

    response = TestClient(main.app).post(
        f"/orders/{order_id}/cancel", data={"cancel_reason": "buyer_requested"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)

    with Session(db) as session:
        membership = session.query(PickWaveOrder).filter_by(order_id=order_id).one()
        assert membership.status == "closed"


def test_the_SYNC_driven_cancel_also_closes_wave_membership(db, wave):
    """The case the wave-40 report suspected. Both surfaces call
    release_order, which is the only place membership is closed -- so the
    sync cannot drift from the manual route. Verified against production:
    order 4140 was cancelled by the sync and its membership is closed."""
    with Session(db) as session:
        wave_row = session.get(PickWave, wave)
        order = make_order(session, label="s-cancel", status="ready_to_pick",
                           allocation_statuses=("allocated",), wave=wave_row)
        session.commit()
        order_id = order.id

    with Session(db) as session:
        result = order_service.reconcile_remote_cancellations(
            session, [],
            lambda _id: {"order": {"latest_fulfillment_status": "refunded"}},
            min_request_interval=0,
        )
        assert result["cancelled"] == 1

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"
        membership = session.query(PickWaveOrder).filter_by(order_id=order_id).one()
        assert membership.status == "closed", "the sync path must close it too"


# --- Cancel is offered wherever the route accepts it -----------------------

@pytest.mark.parametrize("status,allocation", [
    ("ready_to_pick", "allocated"),
    ("picked", "picked"),
    ("packed", "packed"),
])
def test_cancel_is_offered_on_every_cancellable_status(db, status, allocation):
    """Operator decision 2026-09-15, hit live 2026-09-17: the route has
    accepted a picked/packed cancel since v1.161.0, but the button only
    ever rendered for ready_to_pick."""
    with Session(db) as session:
        order = make_order(session, label=f"c-{status}", status=status,
                           allocation_statuses=(allocation,))
        session.commit()
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert f'action="/orders/{order_id}/cancel"' in text
    assert "Cancel &amp; Release Cards" in text
    assert 'name="cancel_reason"' in text, "the reason dropdown comes with it"


def test_cancel_is_not_offered_on_a_shipped_order(db):
    """Those cards are with a customer."""
    with Session(db) as session:
        order = make_order(session, label="shipped", status="shipped",
                           allocation_statuses=("packed",))
        session.commit()
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert f'action="/orders/{order_id}/cancel"' not in text


def test_cancel_is_offered_even_when_the_forward_transition_is_blocked(db):
    """A picked order held up by an unsubmitted exception is exactly the
    order an operator most needs to be able to cancel."""
    with Session(db) as session:
        order = make_order(session, label="blocked", status="picked",
                           allocation_statuses=("exception",))
        session.commit()
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert f'action="/orders/{order_id}/cancel"' in text


def test_the_confirm_text_says_what_happens_to_already_picked_cards(db):
    """A packed order's cards are physically in a box. "Released back to
    available" without "unpack it" would be a half-truth."""
    with Session(db) as session:
        order = make_order(session, label="packed-confirm", status="packed",
                           allocation_statuses=("packed",))
        session.commit()
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "unpack it physically" in text


def test_cancelling_a_picked_order_releases_its_cards(db):
    """The transition itself, not just the button."""
    with Session(db) as session:
        order = make_order(session, label="do-cancel", status="picked",
                           allocation_statuses=("picked",))
        session.commit()
        order_id = order.id
        card_id = session.query(InventoryCard).filter(
            InventoryCard.name.like("Card do-cancel%")).one().id

    TestClient(main.app).post(
        f"/orders/{order_id}/cancel", data={"cancel_reason": "buyer_requested"},
        follow_redirects=False,
    )
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"
        assert session.get(InventoryCard, card_id).status == "available"
