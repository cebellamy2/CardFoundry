"""The pick wave's ship button is offered at the top as well, and the
tracking notice reads as an instruction.

THE OPERATOR, 2026-10-19: on a wave with packed orders the only "Mark
Wave as Shipped" button sat under the whole orders table, so on any real
wave it was off the bottom of the screen -- he could not find it and
concluded it had moved to the top.

And the notice above it ("N orders need a tracking number...") looked
like an error, while in fact it shows before EVERY shipping: tracking is
only saved at the moment the wave ships, so an order that is about to be
given a tracking number always reads as missing one. It now says what to
do and where.

★ BOTH BUTTONS ARE PLAIN SUBMITS INSIDE THE ONE FORM. The form already
wraps the orders table, so "above the table" is the top of the form.
There is no second form and no `form` attribute, which is why the two
cannot submit different things.
"""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)

ORDERS_TAB = "orders"


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shiptop.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory", lambda **kw: [])
    return TestClient(main.app), engine


def build(engine, *, packed=2, needs_tracking=0, method="first_class"):
    """A wave whose orders are packed and ready to ship."""
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        wave = PickWave(label="W1", status="packed", created_at=datetime.now())
        session.add_all([batch, wave])
        session.flush()
        made = []
        for index in range(packed):
            shipping = "ground_advantage" if index < needs_tracking else method
            order = SalesOrder(
                external_order_id=f"u-{index}", external_label=f"70164{index:04d}",
                source="manapool", status="packed", placed_at=datetime.now(),
                shipping_method=shipping, tracking_number=None, shipping_cents=0,
            )
            session.add(order)
            session.flush()
            session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
            identity = {
                "name": f"Card {index}", "set_code": "soc",
                "collector_number": str(index), "scryfall_id": f"sf{index}",
                "mtgjson_id": f"mtg{index}", "language_id": "EN",
                "condition_id": "NM", "finish_id": "NF",
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
            session.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=card.id,
                batch_id=batch.id, status="packed",
            ))
            made.append(order.id)
        session.commit()
        return wave.id, made


def orders_tab(client_engine, wave_id):
    client, _ = client_engine
    return client.get(f"/pick-waves/{wave_id}?tab={ORDERS_TAB}").text


def ship_action(wave_id):
    return f'action="/pick-waves/{wave_id}/ship"'


def ship_form(html, wave_id):
    """The one form that posts to the SHIP route -- the page has other
    pick-wave forms (reopen, pack) and they are not this one."""
    marker = html.index(ship_action(wave_id))
    start = html.rindex("<form", 0, marker)
    return html[start:html.index("</form>", start)]


# --- both buttons, one form ---------------------------------------------

def test_both_ship_buttons_render_inside_the_one_form(client):
    wave_id, _ = build(client[1], packed=2)
    html = orders_tab(client, wave_id)
    form = ship_form(html, wave_id)
    assert form.count("Mark Wave as Shipped") == 2, (
        "the action should be offered at the top and the bottom"
    )
    assert html.count("Mark Wave as Shipped") == 2, "and nowhere else"


def test_there_is_still_exactly_one_ship_form(client):
    """★ Not a second form: a second one would need its own copy of every
    tracking box, and the two could then submit different things."""
    wave_id, _ = build(client[1], packed=3)
    html = orders_tab(client, wave_id)
    assert html.count(ship_action(wave_id)) == 1, "exactly one ship form"
    assert 'form="' not in ship_form(html, wave_id), (
        "no form attribute is needed -- both buttons are physically inside it"
    )


def test_the_top_button_is_above_the_table(client):
    wave_id, _ = build(client[1], packed=3)
    html = orders_tab(client, wave_id)
    first_button = html.index("Mark Wave as Shipped")
    table = html.index('<table class="data-table')
    last_button = html.rindex("Mark Wave as Shipped")
    assert first_button < table < last_button, (
        "one copy above the table, one below"
    )


def test_the_top_button_carries_the_landing_anchor(client):
    wave_id, _ = build(client[1], packed=2)
    html = orders_tab(client, wave_id)
    assert f'id="{main.SHIP_WAVE_TOP_ANCHOR}"' in html
    assert html.index(f'id="{main.SHIP_WAVE_TOP_ANCHOR}"') < html.index(
        '<table class="data-table')


def test_neither_button_renders_without_packed_orders(client):
    """Same condition as before: nothing to ship, no button anywhere."""
    wave_id, _ = build(client[1], packed=0)
    html = orders_tab(client, wave_id)
    assert "Mark Wave as Shipped" not in html
    assert f'id="{main.SHIP_WAVE_TOP_ANCHOR}"' not in html


def test_the_tracking_boxes_are_inside_the_same_form(client):
    """The top button must carry them, which it does by being in the form."""
    wave_id, _ = build(client[1], packed=2, needs_tracking=2)
    form = ship_form(orders_tab(client, wave_id), wave_id)
    assert 'name="tracking_numbers"' in form
    assert 'name="ship_order_ids"' in form


# --- the notice reads as an instruction ---------------------------------

def test_the_notice_says_what_to_do_not_that_something_is_wrong(client):
    wave_id, _ = build(client[1], packed=2, needs_tracking=2)
    html = orders_tab(client, wave_id)
    assert "2 orders need a tracking number." in html
    assert "Enter them on the" in html
    assert "then press Mark Wave as Shipped." in html
    # the old wording blamed the wave
    assert "before this wave can be shipped" not in html


def test_one_order_reads_in_the_singular(client):
    """v2.61.0 fixed the verb: it was "1 order need a tracking number."."""
    html = orders_tab(client, build(client[1], packed=2, needs_tracking=1)[0])
    assert "1 order needs a tracking number." in html
    assert "1 order need a" not in html


def test_the_notice_links_to_the_orders_tab_and_the_top_button(client):
    wave_id, _ = build(client[1], packed=2, needs_tracking=1)
    html = orders_tab(client, wave_id)
    assert f'href="/pick-waves/{wave_id}?tab={ORDERS_TAB}#{main.SHIP_WAVE_TOP_ANCHOR}"' in html


def test_the_notice_keeps_its_per_order_links(client):
    wave_id, orders = build(client[1], packed=2, needs_tracking=2)
    html = orders_tab(client, wave_id)
    for order_id in orders:
        assert f"#wave-order-{order_id}" in html


def test_the_notice_is_not_dressed_as_a_warning(client):
    """It shows before every single shipping, so it must not look like
    something has gone wrong."""
    wave_id, _ = build(client[1], packed=2, needs_tracking=1)
    html = orders_tab(client, wave_id)
    start = html.index("a tracking number.")
    panel = html[html.rindex("<div", 0, start):start]
    assert 'class="notice' in panel
    assert 'class="warning' not in panel


def test_no_notice_when_nothing_needs_tracking(client):
    wave_id, _ = build(client[1], packed=2, needs_tracking=0)
    html = orders_tab(client, wave_id)
    # a phrase unique to the NOTICE: the ship button's own explanation
    # also mentions a tracking number, and that is not the notice
    assert "Enter them on the" not in html
    assert "needs a tracking number." not in html
    assert "need a tracking number." not in html


def test_the_notice_also_shows_on_the_picklist_tab(client):
    """Unchanged: the page opens on Picklist, so the notice has to be
    visible there too or it is invisible exactly when it matters."""
    client_, _ = client
    wave_id, _ = build(client[1], packed=2, needs_tracking=1)
    html = client_.get(f"/pick-waves/{wave_id}").text
    assert "a tracking number." in html
    assert f"#{main.SHIP_WAVE_TOP_ANCHOR}" in html, (
        "from the picklist tab the link must switch tab and land on the button"
    )


# --- shipping through the route is unchanged ----------------------------

def payload(order_ids, tracking):
    """The shape the real form submits: repeated fields, paired by
    position. httpx wants a dict of lists for that, not tuples."""
    return {"ship_order_ids": list(order_ids), "tracking_numbers": list(tracking)}


def test_the_top_button_ships_exactly_as_the_bottom_one_does(client):
    """Both submit the same form, so there is one submission to test --
    and it must still ship."""
    client_, engine = client
    wave_id, orders = build(engine, packed=2)
    response = client_.post(
        f"/pick-waves/{wave_id}/ship",
        data=payload(orders, ["", ""]), follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text[:300]
    with Session(engine) as session:
        statuses = [session.get(SalesOrder, o).status for o in orders]
    assert statuses == ["shipped", "shipped"], statuses


def test_missing_required_tracking_is_still_refused_all_or_nothing(client):
    """★ THE GUARD IS UNCHANGED. A ground_advantage order with no tracking
    rejects the whole action, whichever button was pressed."""
    client_, engine = client
    wave_id, orders = build(engine, packed=2, needs_tracking=1)
    response = client_.post(
        f"/pick-waves/{wave_id}/ship",
        data=payload(orders, ["", ""]), follow_redirects=False,
    )
    with Session(engine) as session:
        statuses = [session.get(SalesOrder, o).status for o in orders]
    assert statuses == ["packed", "packed"], (
        "nothing may ship when a required tracking number is missing"
    )


def test_supplying_the_tracking_number_lets_it_ship(client):
    client_, engine = client
    wave_id, orders = build(engine, packed=2, needs_tracking=1)
    response = client_.post(
        f"/pick-waves/{wave_id}/ship",
        data=payload(orders, ["9400111899223", ""]), follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text[:300]
    with Session(engine) as session:
        statuses = [session.get(SalesOrder, o).status for o in orders]
    assert statuses == ["shipped", "shipped"], statuses
