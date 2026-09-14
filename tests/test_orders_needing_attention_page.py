"""Ticket B (v1.157.0): /orders/shipment-sync-issues broadened from
"Mana Pool Sync Issues" into "Orders Needing Attention", with the existing
sync categories under their own sub-heading and two new sections beneath.

Covers: every section renders (including empty ones), the exception
section is a view of Ticket A's data and offers Ticket A's own actions,
the short/unallocatable section, the URL alias, and the deliberate
decision that the site-wide banner stays scoped to sync failures.
"""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, FulfillmentException, InventoryCard, SalesOrder
from tests.test_fulfillment_exception_reconciliation import (
    remote_line, remote_order, submit,
)
from tests.test_fulfillment_exception_service import seed

PAGE = "/orders/shipment-sync-issues"
ALIAS = "/orders/needs-attention"

SECTIONS = (
    "Mana Pool sync",
    "Fulfillment exceptions awaiting close-out",
    "Short / unallocatable orders",
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'needs-attention.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_exception(session, *, remote_status="shipped"):
    order, item, card, allocation = seed(session)
    exception = submit(session, allocation)
    order.remote_fulfillment_status = remote_status
    session.commit()
    return order, item, card, exception


# --- reframing ----------------------------------------------------------

def test_page_is_reframed_and_no_longer_claims_to_be_sync_only(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get(PAGE)
    assert response.status_code == 200
    assert "<h1>Orders Needing Attention</h1>" in response.text
    assert "Mana Pool Sync Issues" not in response.text
    # the old intro claimed the whole page was about failed pushes
    assert "These orders had a CardFoundry status change" not in response.text


def test_every_section_renders_even_with_no_data_at_all(tmp_path, monkeypatch):
    """An absent section is indistinguishable from a category that does
    not exist. Each one states that it checked and found nothing."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get(PAGE)
    assert response.status_code == 200
    for heading in SECTIONS:
        assert f"<h2>{heading}</h2>" in response.text, heading
    assert "No orders currently have a stuck Mana Pool status sync." in response.text
    assert "No fulfillment exceptions are awaiting close-out." in response.text
    assert "No orders are currently short or awaiting review." in response.text


def test_sections_render_in_the_documented_order(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    text = client.get(PAGE).text
    positions = [text.index(f"<h2>{heading}</h2>") for heading in SECTIONS]
    assert positions == sorted(positions)


# --- URL handling -------------------------------------------------------

def test_original_url_still_serves_the_page_directly(tmp_path, monkeypatch):
    """The canonical path is unchanged, so the banner, the three retry
    routes that redirect back here, and existing tests keep working."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get(PAGE, follow_redirects=False)
    assert response.status_code == 200
    assert "Orders Needing Attention" in response.text


def test_new_alias_redirects_to_the_canonical_path(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    redirect = client.get(ALIAS, follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == PAGE
    assert "Orders Needing Attention" in client.get(ALIAS).text


# --- exception section: a view of Ticket A's data ------------------------

def test_unresolved_exception_appears_with_its_order_and_manapool_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _, card, exception = make_exception(session, remote_status="shipped")
        order_id, card_id = order.id, card.id

    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert "No fulfillment exceptions are awaiting close-out." not in text
    assert f'href="/orders/{order_id}"' in text
    assert f"#{card_id}" in text  # card reference, never a bare id
    assert "missing" in text      # exception type


def test_exception_section_offers_ticket_a_resolve_while_outcome_is_awaiting(tmp_path, monkeypatch):
    """Rendered by the SAME _fulfillment_exception_resolve_action the
    order detail page uses, so the two surfaces cannot drift."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, exception = make_exception(session)
        exception_id = exception.id

    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert f'action="/fulfillment-exceptions/{exception_id}/resolve"' in text
    assert f"/fulfillment-exceptions/{exception_id}/close-out-inventory" not in text


def test_exception_section_offers_close_out_once_the_outcome_is_terminal(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, exception = make_exception(session, remote_status="delivered")
        exception_id, item_id = exception.id, item.id

    with Session(db) as session:
        line = remote_line(session.get(main.OrderItem, item_id), "delivered")
    monkeypatch.setattr(
        main, "get_seller_order",
        lambda external_id: {"order": remote_order(items=[line], status="delivered")},
    )
    client = TestClient(main.app)
    client.post(f"/fulfillment-exceptions/{exception_id}/resolve")

    text = client.get(PAGE).text
    assert f'action="/fulfillment-exceptions/{exception_id}/close-out-inventory"' in text
    assert "Close out inventory record" in text


def test_exception_leaves_the_section_once_its_inventory_is_closed_out(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, exception = make_exception(session, remote_status="delivered")
        exception_id, item_id = exception.id, item.id

    with Session(db) as session:
        line = remote_line(session.get(main.OrderItem, item_id), "delivered")
    monkeypatch.setattr(
        main, "get_seller_order",
        lambda external_id: {"order": remote_order(items=[line], status="delivered")},
    )
    client = TestClient(main.app)
    client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    client.post(f"/fulfillment-exceptions/{exception_id}/close-out-inventory")

    text = client.get(PAGE).text
    assert "No fulfillment exceptions are awaiting close-out." in text
    assert f"/fulfillment-exceptions/{exception_id}/close-out-inventory" not in text


def test_both_surfaces_offer_the_same_action_for_the_same_exception(tmp_path, monkeypatch):
    """The close-out action stays in BOTH places rather than moving: the
    order page is where someone working one order looks, this page is the
    aggregate queue. They share one renderer, so they agree by
    construction."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _, _, exception = make_exception(session)
        order_id, exception_id = order.id, exception.id

    client = TestClient(main.app)
    attention = client.get(PAGE).text
    order_page = client.get(f"/orders/{order_id}").text
    action = f'action="/fulfillment-exceptions/{exception_id}/resolve"'
    assert action in attention
    assert action in order_page


# --- short / unallocatable ----------------------------------------------

def test_short_section_is_empty_and_says_so_when_nothing_qualifies(tmp_path, monkeypatch):
    """Zero orders qualify in production today -- the category must render
    structurally without needing live data."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert "<h2>Short / unallocatable orders</h2>" in text
    assert "No orders are currently short or awaiting review." in text


def test_short_order_appears_with_a_retry_allocation_action(tmp_path, monkeypatch):
    """Unlike "quantity decrease -- no binding", a shortfall DOES have
    something to retry: inventory may have arrived since."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="SHORT-1", external_label="SHORT-1", source="manapool",
            status="short", review_detail="Only 1 of 3 copies available.",
        ))
        session.commit()
        order_id = session.query(SalesOrder).one().id

    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert "No orders are currently short or awaiting review." not in text
    assert "SHORT-1" in text
    assert "Only 1 of 3 copies available." in text
    assert f'action="/orders/{order_id}/approve"' in text
    assert "Retry Allocation" in text


def test_needs_review_orders_are_included_too(tmp_path, monkeypatch):
    """approve_reserved_order accepts both statuses, and "what is stuck?"
    does not distinguish them."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="REVIEW-1", external_label="REVIEW-1", source="manapool",
            status="needs_review", review_detail="Identity could not be resolved.",
        ))
        session.commit()

    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert "REVIEW-1" in text
    assert "Identity could not be resolved." in text


def _short_section(text: str) -> str:
    """Just the short/unallocatable section, so an assertion about it
    cannot be satisfied (or broken) by a row in a section above it."""
    return text[text.index("<h2>Short / unallocatable orders</h2>"):]


def test_shipped_orders_never_appear_in_the_short_section(tmp_path, monkeypatch):
    """A shipped order with no recorded sync DOES belong on this page --
    in the Mana Pool sync section. It must not also be reported as short,
    which is a different problem with a different action."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="DONE-1", external_label="DONE-1",
            source="manapool", status="shipped",
        ))
        session.commit()

    client = TestClient(main.app)
    text = client.get(PAGE).text
    assert "DONE-1" in text  # present, in the sync section
    short = _short_section(text)
    assert "DONE-1" not in short
    assert "No orders are currently short or awaiting review." in short


# --- banner decision ----------------------------------------------------

def test_banner_stays_scoped_to_sync_and_ignores_the_exception_backlog(tmp_path, monkeypatch):
    """Ticket B decision: the banner is ambient and danger-styled on every
    page. Folding in a known backlog would pin it red permanently until
    Ticket C drains it, turning a rare, urgent alert into wallpaper."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_exception(session)  # an unresolved exception, but no sync failure

    client = TestClient(main.app)
    text = client.get("/inventory").text
    assert "failed to sync to Mana Pool" not in text


def test_banner_still_fires_and_counts_only_sync_failures(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_exception(session)  # must NOT be counted
        session.add(SalesOrder(
            external_order_id="STUCK-1", source="manapool", status="shipped",
            shipped_at=datetime(2026, 9, 1, 12, 0),
            mana_pool_shipment_synced_at=None, mana_pool_shipment_released_at=None,
        ))
        session.commit()

    client = TestClient(main.app)
    text = client.get("/inventory").text
    assert "<strong>1 order failed to sync to Mana Pool.</strong>" in text
    assert f'href="{PAGE}"' in text
