"""The pick wave's Fulfillment Exceptions table is grouped by order.

THE OPERATOR, 2026-10-16: "Exceptions on the pickwave screen should be
grouped by order. When you go on Mana Pool you have to submit an exception
on the order it is on specifically, and it is more efficient to know all
the exceptions by order."

Backed by production: of the 11 orders that have ever carried 2+
exceptions, 9 had every one of them submitted within the SAME MINUTE. He
is already working order by order; the page just was not arranged that
way.

DISPLAY ONLY. Same rows, same per-card buttons, same anchors, no new
write path and no state change. The order-level "Mark remaining N as
submitted" button is slice 2 and lives in
tests/test_wave_exception_order_level_submit.py, together with the count
wording that slice changed.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, Batch, FulfillmentException, InventoryCard, OrderItem,
    PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)

# Real Mana Pool order numbers in the shape production uses.
ORDER_A_LABEL = "701646-2472514"
ORDER_B_LABEL = "701450-2471898"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave-exceptions.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def build_wave(engine):
    """A wave with TWO exceptions on order A and ONE on order B.

    Exactly the shape the operator described, and the shape the old flat
    table made hard to read.
    """
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        wave = PickWave(label="W1", status="active")
        session.add_all([batch, wave])
        session.flush()

        made = {}
        for label, count in ((ORDER_A_LABEL, 2), (ORDER_B_LABEL, 1)):
            order = SalesOrder(
                external_order_id=f"ext-{label}", external_label=label,
                source="manapool", status="ready_to_pick",
            )
            session.add(order)
            session.flush()
            session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
            made[label] = {"order": order, "cards": [], "exceptions": []}
            for index in range(count):
                # The order line and the physical card must agree on all
                # eight identity fields -- mark_fulfillment_exception
                # refuses otherwise, which is the real guard and worth
                # satisfying honestly rather than bypassing.
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
                # Through the REAL service, not hand-built rows. A
                # hand-built fixture missed the card's own
                # inventory_exception_state projection, and the submit
                # route then refused with "Card is not carrying an
                # unresolved exception projection" -- a shape production
                # never produces. Building it the way the app does means
                # the fixture cannot drift from the real thing.
                exception = mark_fulfillment_exception(
                    session, allocation.id, "missing", note="reported in test",
                )
                session.flush()
                made[label]["cards"].append(card.id)
                made[label]["exceptions"].append(exception.id)
        session.commit()
        return wave.id, {
            label: {"order_id": data["order"].id, "cards": data["cards"],
                    "exceptions": data["exceptions"]}
            for label, data in made.items()
        }


def group_html(html, order_id):
    """Everything from one order's heading up to the next heading."""
    start = html.index(f'id="exception-order-{order_id}"')
    rest = html[start:]
    next_heading = re.search(r'id="exception-order-\d+"', rest[1:])
    return rest[:next_heading.start()] if next_heading else rest


# --- the grouping the operator asked for --------------------------------

def test_each_order_gets_its_own_heading_with_its_mana_pool_number(db):
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert html.count('class="exception-order-heading"') == 2, (
        "expected one heading per order"
    )
    for label, data in made.items():
        heading = group_html(html, data["order_id"])
        assert label in heading, f"{label} is not in its own heading"


def test_each_order_only_shows_its_own_cards(db):
    """The point of grouping: the cards under an order's heading are that
    order's cards and nobody else's."""
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for label, data in made.items():
        heading = group_html(html, data["order_id"])
        for exception_id in data["exceptions"]:
            assert f'id="exception-{exception_id}"' in heading, (
                f"exception {exception_id} is missing from {label}'s group"
            )
        others = [
            exception_id
            for other_label, other in made.items() if other_label != label
            for exception_id in other["exceptions"]
        ]
        for exception_id in others:
            assert f'id="exception-{exception_id}"' not in heading, (
                f"exception {exception_id} leaked into {label}'s group"
            )


def test_the_count_reads_none_submitted_when_none_are(db):
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "0 of 2 submitted" in group_html(html, made[ORDER_A_LABEL]["order_id"])
    assert "0 of 1 submitted" in group_html(html, made[ORDER_B_LABEL]["order_id"])


def test_the_count_follows_the_real_submission_states(db):
    """★ THE PARTIAL CASE, which is the normal one: a new exception lands
    on an order whose others are already submitted."""
    wave_id, made = build_wave(db)
    with Session(db) as session:
        first = session.get(FulfillmentException, made[ORDER_A_LABEL]["exceptions"][0])
        first.submission_state = "submitted"
        session.commit()

    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "1 of 2 submitted" in group_html(html, made[ORDER_A_LABEL]["order_id"])
    assert "0 of 1 submitted" in group_html(html, made[ORDER_B_LABEL]["order_id"])


def test_an_order_appears_where_its_first_exception_falls(db):
    """Insertion order, not sorted. Reporting a second exception on an
    order must not make that order jump position under the operator
    mid-pick."""
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    first = html.index(f'id="exception-order-{made[ORDER_A_LABEL]["order_id"]}"')
    second = html.index(f'id="exception-order-{made[ORDER_B_LABEL]["order_id"]}"')
    assert first < second


# --- nothing else may have changed --------------------------------------

def test_every_per_card_button_is_still_rendered(db):
    """DISPLAY ONLY. The per-card actions are untouched by this slice."""
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for exception_id in made[ORDER_A_LABEL]["exceptions"]:
        assert f'action="/fulfillment-exceptions/{exception_id}/submitted"' in html, (
            "the per-card Submitted button must still be there -- the "
            "order-level one is slice 2 and is not built yet"
        )


def test_the_heading_still_links_to_the_order_page(db):
    """The order number in the heading is a link to the order, which is
    how he gets from a wave to the order's own page. Slice 2 added a
    button between the heading and its table; the link above it stayed."""
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for data in made.values():
        heading = group_html(html, data["order_id"])
        assert f'href="/orders/{data["order_id"]}"' in heading


def test_every_exception_row_keeps_its_anchor(db):
    """v2.48.0 sends the operator back to #exception-<id> after acting
    from the table. Grouping must not drop those ids."""
    wave_id, made = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for data in made.values():
        for exception_id in data["exceptions"]:
            assert f'id="exception-{exception_id}"' in html


def test_a_single_per_card_action_still_returns_to_its_row(db):
    """The whole v2.48.0 return-to-row behaviour, exercised through the
    real route: acting on ONE card from the exception table still lands
    on that card's own row, not somewhere else."""
    wave_id, made = build_wave(db)
    exception_id = made[ORDER_A_LABEL]["exceptions"][0]
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "submitted by hand", "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}#exception-{exception_id}"
    )
    # and the target it names is really on the page it points at
    page = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert f'id="exception-{exception_id}"' in page.text


def test_the_table_loads_orders_in_one_query_not_one_per_row(db):
    """The page's statement count is pinned by its own tests; grouping
    must not add a query per exception."""
    wave_id, _ = build_wave(db)
    from sqlalchemy import event

    statements = []
    engine = main.engine

    def record(conn, cursor, statement, parameters, context, executemany):
        if "sales_orders" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        TestClient(main.app).get(f"/pick-waves/{wave_id}")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    prefetches = [s for s in statements if " IN (" in s.upper()]
    assert prefetches, "expected a batched IN (...) load of the orders"
