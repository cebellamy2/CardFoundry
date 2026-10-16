"""Slice 2: the order-level "Mark remaining N as submitted" button.

THE OPERATOR, slice 2: on Mana Pool an exception is submitted on the order
it belongs to, so the real unit of work is the order, not the card. Slice 1
grouped the table by order; this gives each group one button and one note.

Backed by production: of the orders that have ever carried two or more
exceptions, nearly all had every one of them submitted inside the same
minute. He was already doing this by hand, one order at a time.

WHAT THE BUTTON IS. A local mark, nothing else. The service it calls
"never calls ManaPool and never commits" -- it records that he already
submitted by hand on their site. So batching carries no marketplace risk,
and `test_no_mana_pool_call_is_made` holds that line.

ALL OR NOTHING. Submitting is irreversible (there is no un-submit
anywhere in the codebase, deliberately), so a half-marked order would be
worse than an unmarked one: he could not tell from the page which half
went through. One refusal leaves the whole group untouched.

FIXTURES GO THROUGH THE REAL SERVICES. `mark_fulfillment_exception` to
create, and the real route to pre-submit. Hand-built rows miss the card's
own inventory_exception_state projection and the eight-field identity
match, and produce a shape production never has.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import manapool_service
from fulfillment_exception_constants import FULFILLMENT_EXCEPTION_SUBMITTED_EVENT
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, Batch, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    OrderItem, PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)

# Real Mana Pool order numbers in the shape production uses.
ORDER_A_LABEL = "701646-2472514"
ORDER_B_LABEL = "701450-2471898"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'order-level-submit.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])

    # Every Mana Pool call in the app funnels through this module's
    # httpx.Client. Replacing it means ANY attempt to reach the
    # marketplace fails the test loudly rather than silently succeeding
    # against a fake. This is the guard, not a convenience.
    class NoManaPool:
        def Client(self, *args, **kwargs):
            raise AssertionError(
                "the order-level submit must never contact Mana Pool"
            )

    monkeypatch.setattr(manapool_service, "httpx", NoManaPool())
    return engine


def build_wave(engine, shape=((ORDER_A_LABEL, 3), (ORDER_B_LABEL, 1))):
    """A wave carrying `shape` -- (order label, exception count) pairs.

    Three on order A is the case the operator cares about: more than one
    to mark, and room for one of them to already be submitted.
    """
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        wave = PickWave(label="W1", status="active")
        session.add_all([batch, wave])
        session.flush()

        made = {}
        for label, count in shape:
            order = SalesOrder(
                external_order_id=f"ext-{label}", external_label=label,
                source="manapool", status="ready_to_pick",
            )
            session.add(order)
            session.flush()
            session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
            made[label] = {"order_id": order.id, "cards": [], "exceptions": [],
                           "allocations": []}
            for index in range(count):
                # The order line and the physical card must agree on all
                # eight identity fields; the service refuses otherwise.
                identity = {
                    "name": f"Card {label}-{index}",
                    "set_code": "soc",
                    "collector_number": str(100 + index),
                    "scryfall_id": f"sf-{label}-{index}",
                    "mtgjson_id": f"mtg-{label}-{index}",
                    "language_id": "EN",
                    "condition_id": "NM",
                    "finish_id": "NF",
                }
                item = OrderItem(order_id=order.id, quantity=1, **identity)
                session.add(item)
                session.flush()
                card = InventoryCard(
                    batch_id=batch.id, condition="Near Mint", finish="normal",
                    status="reserved", current_price=1.0, **identity,
                )
                session.add(card)
                session.flush()
                allocation = PickAllocation(
                    order_item_id=item.id, inventory_card_id=card.id,
                    batch_id=batch.id, status="allocated",
                )
                session.add(allocation)
                session.flush()
                exception = mark_fulfillment_exception(
                    session, allocation.id, "missing", note="reported in test",
                )
                session.flush()
                made[label]["cards"].append(card.id)
                made[label]["allocations"].append(allocation.id)
                made[label]["exceptions"].append(exception.id)
        session.commit()
        return wave.id, made


def group_html(html, order_id):
    """Everything from one order's heading up to the next heading."""
    start = html.index(f'id="exception-order-{order_id}"')
    rest = html[start:]
    next_heading = re.search(r'id="exception-order-\d+"', rest[1:])
    return rest[:next_heading.start()] if next_heading else rest


def submission_states(engine, exception_ids):
    with Session(engine) as session:
        return [
            session.get(FulfillmentException, exception_id).submission_state
            for exception_id in exception_ids
        ]


def submitted_events(engine, exception_ids):
    with Session(engine) as session:
        return session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.fulfillment_exception_id.in_(exception_ids),
            FulfillmentExceptionEvent.event_type == FULFILLMENT_EXCEPTION_SUBMITTED_EVENT,
        ).all()


def group_tags(engine, exception_ids):
    tags = []
    for event in submitted_events(engine, exception_ids):
        metadata = json.loads(event.operator_metadata or "null")
        if isinstance(metadata, dict) and metadata.get("order_group_submission"):
            tags.append(metadata["order_group_submission"])
    return tags


def submit_one_by_hand(client, exception_id, wave_id):
    """The per-card button, through the real route."""
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "submitted by hand", "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    return response


def press_order_button(client, order_id, wave_id, note="whole order submitted"):
    return client.post(
        f"/orders/{order_id}/fulfillment-exceptions/submitted",
        data={"note": note, "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )


# --- what the button does -----------------------------------------------

def test_it_marks_only_the_remaining_ones_and_the_order_ends_fully_submitted(db):
    """★ THE CASE THE OPERATOR DESCRIBED: three exceptions on one order,
    one of them already submitted. The click marks exactly the two that
    were left, and afterwards the whole order reads submitted."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    submit_one_by_hand(client, order["exceptions"][0], wave_id)
    assert submission_states(db, order["exceptions"]) == [
        "submitted", "needs_submission", "needs_submission",
    ]

    response = press_order_button(client, order["order_id"], wave_id)
    assert response.status_code == 303, response.text[:400]

    assert submission_states(db, order["exceptions"]) == [
        "submitted", "submitted", "submitted",
    ]
    # Exactly two were marked BY THE CLICK -- the already-submitted one
    # was skipped, not re-marked, so it has no second event.
    assert len(submitted_events(db, order["exceptions"])) == 3
    assert len(group_tags(db, order["exceptions"])) == 2


def test_another_orders_exceptions_are_untouched(db):
    wave_id, made = build_wave(db)
    client = TestClient(main.app)
    press_order_button(client, made[ORDER_A_LABEL]["order_id"], wave_id)

    other = made[ORDER_B_LABEL]["exceptions"]
    assert submission_states(db, other) == ["needs_submission"]
    assert submitted_events(db, other) == []


def test_every_row_marked_by_one_click_carries_the_same_group_tag(db):
    """Traceable as ONE operator action rather than N coincidental ones."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    press_order_button(TestClient(main.app), order["order_id"], wave_id)

    tags = group_tags(db, order["exceptions"])
    assert len(tags) == 3, "every marked exception should carry a tag"
    assert len(set(tags)) == 1, f"the group tag must be shared, got {set(tags)}"
    assert str(order["order_id"]) in tags[0]


def test_one_failure_rolls_back_the_whole_group(db):
    """★ ALL OR NOTHING. The middle exception's allocation is moved out of
    exception state, which is a refusal the real service enforces. The
    first one would already have been marked -- it must not stay marked,
    because submitting cannot be undone."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    with Session(db) as session:
        allocation = session.get(PickAllocation, order["allocations"][1])
        allocation.status = "allocated"
        session.commit()

    response = press_order_button(TestClient(main.app), order["order_id"], wave_id)
    assert response.status_code == 409, response.status_code
    assert "Nothing on this order was marked" in response.text

    assert submission_states(db, order["exceptions"]) == [
        "needs_submission", "needs_submission", "needs_submission",
    ]
    assert submitted_events(db, order["exceptions"]) == []


def test_a_double_press_is_harmless(db):
    """The button is hidden once the count reaches zero, but a stale page,
    a double click or a back-and-resubmit can still post. Nothing pending
    is not an error."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    first = press_order_button(client, order["order_id"], wave_id)
    assert first.status_code == 303
    events_after_first = len(submitted_events(db, order["exceptions"]))

    second = press_order_button(client, order["order_id"], wave_id)
    assert second.status_code == 303, second.text[:400]
    assert second.headers["location"] == first.headers["location"]
    assert submission_states(db, order["exceptions"]) == [
        "submitted", "submitted", "submitted",
    ]
    assert len(submitted_events(db, order["exceptions"])) == events_after_first, (
        "a second press must not add another submitted event"
    )


def test_the_click_returns_to_that_orders_group(db):
    """Below the sticky nav, at the group he was looking at -- the third
    return target. v2.46.0's scroll-margin on [id] clears the nav."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    response = press_order_button(TestClient(main.app), order["order_id"], wave_id)
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}#exception-order-{order['order_id']}"
    )
    # and the anchor it names really is on the page it points at
    page = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert f'id="exception-order-{order["order_id"]}"' in page


def test_without_a_wave_it_returns_to_the_order_page(db):
    """The order page carries no wave field, so its behaviour is its own."""
    _wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    response = TestClient(main.app).post(
        f"/orders/{order['order_id']}/fulfillment-exceptions/submitted",
        data={"note": "from the order page"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order['order_id']}"


def test_no_mana_pool_call_is_made(db):
    """LOCAL MARK ONLY. The fixture makes any Mana Pool call raise, so
    this passing is the assertion."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    response = press_order_button(TestClient(main.app), order["order_id"], wave_id)
    assert response.status_code == 303
    assert submission_states(db, order["exceptions"]) == [
        "submitted", "submitted", "submitted",
    ]


# --- the label and the count --------------------------------------------

def test_the_button_names_the_number_it_will_act_on(db):
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Mark remaining 3 as submitted" in group_html(
        html, made[ORDER_A_LABEL]["order_id"],
    )
    assert "Mark remaining 1 as submitted" in group_html(
        html, made[ORDER_B_LABEL]["order_id"],
    )


def test_the_label_follows_what_is_actually_left(db):
    """One already submitted means the button promises two, not three."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    submit_one_by_hand(client, order["exceptions"][0], wave_id)
    group = group_html(client.get(f"/pick-waves/{wave_id}").text, order["order_id"])
    assert "Mark remaining 2 as submitted" in group
    assert "1 of 3 submitted" in group


def test_the_button_is_hidden_when_nothing_remains(db):
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    press_order_button(client, order["order_id"], wave_id)

    group = group_html(client.get(f"/pick-waves/{wave_id}").text, order["order_id"])
    assert "Mark remaining" not in group
    assert "3 of 3 submitted" in group
    # the other order still has its own button
    other = group_html(
        client.get(f"/pick-waves/{wave_id}").text, made[ORDER_B_LABEL]["order_id"],
    )
    assert "Mark remaining 1 as submitted" in other


def test_the_per_card_button_is_still_there_as_the_fallback(db):
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for exception_id in made[ORDER_A_LABEL]["exceptions"]:
        assert f'action="/fulfillment-exceptions/{exception_id}/submitted"' in html


def test_the_per_card_button_still_works(db):
    """Keeping it rendered is not the same as keeping it working."""
    wave_id, made = build_wave(db)
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    submit_one_by_hand(client, order["exceptions"][1], wave_id)
    assert submission_states(db, order["exceptions"]) == [
        "needs_submission", "submitted", "needs_submission",
    ]


# --- not_required is named, not counted ---------------------------------

def set_not_required(engine, exception_id):
    """A substitution is what produces this state in production
    (fulfillment_substitution_service). These are display assertions and
    the button only ever selects needs_submission, so the state itself is
    what matters here, not the path that set it."""
    with Session(engine) as session:
        session.get(FulfillmentException, exception_id).submission_state = "not_required"
        session.commit()


def test_not_required_is_named_separately_and_left_out_of_the_count(db):
    """★ SLICE 1 GOT THIS WRONG: not_required sat in the denominator, so
    an order with one submitted and one substituted read "1 of 2
    submitted" -- which looks like outstanding work and is not."""
    wave_id, made = build_wave(db, shape=((ORDER_A_LABEL, 2),))
    order = made[ORDER_A_LABEL]
    client = TestClient(main.app)
    set_not_required(db, order["exceptions"][1])
    submit_one_by_hand(client, order["exceptions"][0], wave_id)

    group = group_html(client.get(f"/pick-waves/{wave_id}").text, order["order_id"])
    assert "1 of 1 submitted" in group
    assert "1 not required" in group
    assert "1 of 2 submitted" not in group
    assert "Mark remaining" not in group, "nothing is left to submit"


def test_an_order_with_only_not_required_says_nothing_to_submit(db):
    wave_id, made = build_wave(db, shape=((ORDER_A_LABEL, 1),))
    order = made[ORDER_A_LABEL]
    set_not_required(db, order["exceptions"][0])

    group = group_html(
        TestClient(main.app).get(f"/pick-waves/{wave_id}").text, order["order_id"],
    )
    assert "Nothing to submit" in group
    assert "1 not required" in group
    assert "0 of" not in group, "a fraction here is exactly the wrong wording"
    assert "Mark remaining" not in group


def test_an_order_with_no_not_required_keeps_the_plain_count(db):
    """The suffix appears only when there is something to name."""
    wave_id, made = build_wave(db)
    group = group_html(
        TestClient(main.app).get(f"/pick-waves/{wave_id}").text,
        made[ORDER_A_LABEL]["order_id"],
    )
    assert "0 of 3 submitted" in group
    assert "not required" not in group


# --- the anchor has to clear BOTH sticky layers -------------------------

def test_the_anchor_clears_both_sticky_layers_not_just_the_nav(db):
    """★ FOUND BY MEASURING, not by reading the source. The shared [id]
    scroll-margin clears ONE sticky layer, and this page has two: the
    site nav, and the wave summary strip that sticks directly beneath it.
    The order heading was landing at 120px with the strip covering
    104-155px, so the click returned him to a heading he could not see.

    This also silently affected the exception-row anchor the per-card
    buttons return to. Both clear both layers now."""
    wave_id, _ = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "body:has(.wave-summary-sticky) [id]" in html, (
        "the two-layer anchor rule is gone; anchors on this page will "
        "land underneath the summary strip again"
    )
    assert "--cf-wave-summary-height" in html
