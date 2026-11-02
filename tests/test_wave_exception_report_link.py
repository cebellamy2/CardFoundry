"""A 'Report on Mana Pool' link beside each order in a wave's exceptions.

Mana Pool has no API for submitting a fulfillment exception, so the
operator reports each one by hand on the Mana Pool ORDER page. The pick
wave already groups exceptions under one heading per order; this puts the
way to that order's Mana Pool page right on the heading.

The URL is built in one place, _manapool_order_url, which Order Detail's
existing "View this order on Mana Pool" link now goes through too. Its
rule: a Mana Pool order with a non-empty external_order_id. A manual order
stores the operator's typed reference in that same column with source
"simulation", and a link built from it would open a Mana Pool page for an
order that does not exist -- so it gets no link at all.

DISPLAY ONLY: no write anywhere, no new JavaScript.
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
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)
from order_report_service import store_reports

# The operator's own example, 2026-10-28: display order 730646-2573613 is
# Mana Pool order 8abdf222-... on their site.
ORDER_A_UUID = "8abdf222-151d-4b51-b6c0-aa1669995808"
ORDER_A_LABEL = "730646-2573613"
ORDER_B_UUID = "0f5c1e7a-2b9d-4c3e-8a61-5d7e9b2c4f10"
ORDER_B_LABEL = "701450-2471898"
# What the manual-order route stores: the typed reference, verbatim, in
# external_order_id, with source "simulation" and no external_label.
LOCAL_REFERENCE = "LOCAL-SALE-42"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave-report-link.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def _add_exception(session, batch, order, key):
    """One order line, its reserved card, an allocation, and an exception
    raised through the REAL service -- a hand-built exception row misses
    the card's own exception projection and produces a shape production
    never does."""
    identity = {
        "name": f"Card {key}",
        "set_code": "soc",
        "collector_number": "101",
        "scryfall_id": f"sf-{key}",
        "mtgjson_id": f"mtg-{key}",
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
    mark_fulfillment_exception(session, allocation.id, "missing", note="reported in test")
    session.flush()


def build_wave(engine):
    """Two Mana Pool orders and one manual order, each with an exception,
    all in one wave. Every order respects the NOT NULL external_order_id."""
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        wave = PickWave(label="W1", status="active")
        session.add_all([batch, wave])
        session.flush()
        orders = {
            "a": SalesOrder(
                external_order_id=ORDER_A_UUID, external_label=ORDER_A_LABEL,
                source="manapool", status="ready_to_pick",
            ),
            "b": SalesOrder(
                external_order_id=ORDER_B_UUID, external_label=ORDER_B_LABEL,
                source="manapool", status="ready_to_pick",
            ),
            "local": SalesOrder(
                external_order_id=LOCAL_REFERENCE,
                source="simulation", status="ready_to_pick",
            ),
        }
        for key, order in orders.items():
            session.add(order)
            session.flush()
            session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
            _add_exception(session, batch, order, key)
        session.commit()
        return wave.id, {key: order.id for key, order in orders.items()}


def heading_html(html, order_id):
    """Just one order's h3 heading."""
    start = html.index(f'id="exception-order-{order_id}"')
    return html[start:html.index("</h3>", start)]


def _report_payload(report_id):
    """A Mana Pool report payload in the shape the sync stores."""
    return {"reports": [{
        "report_id": report_id,
        "order_reported_issues": {
            "reporter_role": "buyer",
            "proposed_remediation_method": "cancellation",
            "created_at": "2026-10-28T12:00:00Z",
            "charges": [{"seller_charge_cents": 252, "payout_id": "p1"}],
        },
    }]}


def build_order_with_report(engine, **order_fields):
    """An order carrying a Mana Pool report, stored through the real
    service, so Order Detail renders its 'Mana Pool report' section."""
    with Session(engine) as session:
        order = SalesOrder(status="shipped", **order_fields)
        session.add(order)
        session.flush()
        store_reports(session, order, _report_payload("107395"))
        session.commit()
        return order.id


# --- the pick wave ------------------------------------------------------

def test_each_mana_pool_order_heading_links_to_its_mana_pool_page(db):
    wave_id, ids = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for key, uuid in (("a", ORDER_A_UUID), ("b", ORDER_B_UUID)):
        heading = heading_html(html, ids[key])
        links = re.findall(r'<a [^>]*>Report on Mana Pool</a>', heading)
        assert links == [
            f'<a href="https://manapool.com/seller/orders/{uuid}" '
            f'target="_blank" rel="noopener">Report on Mana Pool</a>'
        ], heading


def test_the_link_uses_the_mana_pool_uuid_not_the_display_number(db):
    """The operator said the link must use the long UUID; the display
    number is what the heading shows, not what Mana Pool's URL takes."""
    wave_id, ids = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    heading = heading_html(html, ids["a"])
    assert f"/seller/orders/{ORDER_A_LABEL}" not in heading
    assert f"/seller/orders/{ORDER_A_UUID}" in heading


def test_a_manual_order_gets_no_mana_pool_link(db):
    """★ The v1 bug. A simulation order's typed reference is not a Mana
    Pool id, so a link built from it would be dead."""
    wave_id, ids = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    heading = heading_html(html, ids["local"])
    assert LOCAL_REFERENCE in heading, "the manual order's heading is missing"
    assert "Report on Mana Pool" not in heading
    assert "manapool.com" not in heading
    assert f"/seller/orders/{LOCAL_REFERENCE}" not in html


def test_one_report_link_per_mana_pool_order_and_none_bare(db):
    wave_id, _ = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert html.count(">Report on Mana Pool</a>") == 2
    assert 'href="https://manapool.com/seller/orders/"' not in html


def test_the_existing_heading_is_unchanged(db):
    """The order link, its label and the count stay exactly as they were;
    the new link only follows them."""
    wave_id, ids = build_wave(db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    for key, label in (("a", ORDER_A_LABEL), ("b", ORDER_B_LABEL), ("local", LOCAL_REFERENCE)):
        heading = heading_html(html, ids[key])
        assert (
            f'<a href="/orders/{ids[key]}">{label}</a>\n'
            f'                    <span class="exception-order-count">0 of 1 submitted</span>'
        ) in heading, heading


# --- Order Detail, through the same helper ------------------------------

def test_order_detail_link_for_a_mana_pool_order_is_unchanged(db):
    """Byte-for-byte the markup Order Detail rendered before the helper."""
    order_id = build_order_with_report(
        db, external_order_id=ORDER_A_UUID, external_label=ORDER_A_LABEL,
        source="manapool",
    )
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Mana Pool report" in html
    assert (
        "</div>\n"
        f'        <p><a href="https://manapool.com/seller/orders/{ORDER_A_UUID}" '
        'target="_blank" rel="noopener">View this order on Mana Pool</a></p>\n'
        "    </section>"
    ) in html


def test_order_detail_gives_a_manual_order_no_mana_pool_link(db):
    """Same rule as the wave: the report section still renders, the dead
    link does not."""
    order_id = build_order_with_report(
        db, external_order_id=LOCAL_REFERENCE, source="simulation",
    )
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Mana Pool report" in html, "the report section itself must still show"
    assert "View this order on Mana Pool" not in html
    assert "/seller/orders/" not in html


# --- the helper's rule, directly ----------------------------------------

@pytest.mark.parametrize("order, expected", [
    (None, None),
    (SalesOrder(source="manapool", external_order_id=""), None),
    (SalesOrder(source="manapool", external_order_id="   "), None),
    (SalesOrder(source="simulation", external_order_id=LOCAL_REFERENCE), None),
    (SalesOrder(source="manapool", external_order_id=ORDER_A_UUID),
     f"https://manapool.com/seller/orders/{ORDER_A_UUID}"),
    (SalesOrder(source="manapool", external_order_id='x"<y'),
     "https://manapool.com/seller/orders/x&quot;&lt;y"),
])
def test_manapool_order_url_rule(order, expected):
    assert main._manapool_order_url(order) == expected
