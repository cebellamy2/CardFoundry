"""Slice 2: the "Cancelled to match Mana Pool" section.

The framing matters as much as the content. Mana Pool exposes no pending
or requested cancellation state -- a cancellation only ever reaches
CardFoundry after the fact, as "refunded". So this section reports what
already happened and what the sync did about it, and must never read as a
queue of requests awaiting a decision.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, FulfillmentException, InventoryCard, OrderCancellation, SalesOrder
from order_service import reconcile_remote_cancellations
from tests.test_cancellation_reconciliation import exception_line, loader_returning, open_order

PAGE = "/orders/shipment-sync-issues"
HEADING = "<h2>Cancelled to match Mana Pool</h2>"
EMPTY = "No orders have been cancelled by the Mana Pool sync recently."


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'slice2.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def sync_cancel(session, **kw):
    return reconcile_remote_cancellations(
        session, [], loader_returning("refunded"), min_request_interval=0, **kw
    )


# --- structure ----------------------------------------------------------

def test_section_renders_with_an_explicit_none_line_when_empty(db):
    text = TestClient(main.app).get(PAGE).text
    assert HEADING in text
    assert EMPTY in text


def test_heading_carries_no_count_matching_the_other_sections(db):
    """None of the existing sections count in their heading; this one
    must not be the odd one out."""
    text = TestClient(main.app).get(PAGE).text
    assert HEADING in text
    for other in ("<h2>Mana Pool sync</h2>",
                  "<h2>Fulfillment exceptions awaiting close-out</h2>",
                  "<h2>Short / unallocatable orders</h2>"):
        assert other in text


def test_the_table_is_scroll_wrapped_so_the_page_never_scrolls_sideways(db):
    with Session(db) as session:
        open_order(session, external="mp-scroll")
    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    section = text[text.index(HEADING):]
    section = section[:section.index("<h2>", len(HEADING))]
    assert 'class="data-table-scroll"' in section


def test_the_section_never_implies_a_pending_request(db):
    """Mana Pool has no pending-cancellation state. Copy that suggested a
    decision was awaited would be describing something that cannot exist."""
    text = TestClient(main.app).get(PAGE).text
    section = text[text.index(HEADING):]
    section = section[:section.index("<h2>", len(HEADING))]
    assert "Nothing here is waiting for a decision" in section
    for forbidden in ("pending cancellation", "cancellation request", "Approve", "Decline"):
        assert forbidden not in section


# --- content ------------------------------------------------------------

def test_a_sync_cancelled_order_appears_with_its_per_line_outcome(db):
    with Session(db) as session:
        order, _, card, _ = open_order(session, external="mp-shelf")
        order_id, card_id = order.id, card.id

    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    assert EMPTY not in text
    assert f'href="/orders/{order_id}"' in text
    assert "mp-shelf" in text
    assert f"#{card_id}" in text
    assert "released to available" in text


def test_an_exception_line_reads_as_resolved_with_the_card_left_alone(db):
    with Session(db) as session:
        order, _, shelf, _ = open_order(session, external="mp-both")
        exception, missing, _ = exception_line(session, order)
        missing_id = missing.id

    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    assert "released to available" in text
    assert "exception resolved by the refund" in text
    assert "card left removed" in text
    assert f"#{missing_id}" in text


def test_rows_are_newest_first(db):
    with Session(db) as session:
        open_order(session, external="mp-older")
    with Session(db) as session:
        sync_cancel(session)
    with Session(db) as session:
        record = session.query(OrderCancellation).one()
        record.created_at = datetime.now() - timedelta(days=2)
        session.commit()

    with Session(db) as session:
        open_order(session, external="mp-newer")
    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    assert text.index("mp-newer") < text.index("mp-older")


def test_a_manual_cancellation_is_not_listed_here(db):
    """This section is specifically what the sync did on its own."""
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-byhand")
        order_id = order.id

    TestClient(main.app).post(f"/orders/{order_id}/cancel",
                              data={"cancel_reason": "buyer_requested"})

    text = TestClient(main.app).get(PAGE).text
    assert EMPTY in text
    assert "mp-byhand" not in text


# --- age-out ------------------------------------------------------------

def test_rows_age_out_so_the_section_never_becomes_a_history_list(db):
    with Session(db) as session:
        open_order(session, external="mp-ancient")
    with Session(db) as session:
        sync_cancel(session)
    with Session(db) as session:
        record = session.query(OrderCancellation).one()
        record.created_at = datetime.now() - timedelta(
            days=main.CANCELLED_BY_REMOTE_VISIBLE_DAYS + 1,
        )
        session.commit()

    text = TestClient(main.app).get(PAGE).text
    assert "mp-ancient" not in text
    assert EMPTY in text


def test_ageing_out_hides_the_row_without_touching_the_audit_record(db):
    """The row leaves the page; the immutable record stays."""
    with Session(db) as session:
        open_order(session, external="mp-kept")
    with Session(db) as session:
        sync_cancel(session)
    with Session(db) as session:
        record = session.query(OrderCancellation).one()
        record.created_at = datetime.now() - timedelta(days=90)
        session.commit()

    TestClient(main.app).get(PAGE)

    with Session(db) as session:
        assert session.query(OrderCancellation).count() == 1


# --- the shared action --------------------------------------------------

def test_add_back_is_offered_here_for_a_card_still_removed(db):
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-addback2")
        exception, card, _ = exception_line(session, order)
        card_id = card.id

    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    assert "Add Back To Inventory" in text
    assert f'action="/inventory/{card_id}/un-remove/preview"' in text


def test_both_surfaces_offer_the_identical_action_for_the_same_line(db):
    """Same pattern Ticket B used for close-out: one shared renderer, so
    Order Detail and this page cannot drift about when the action appears
    or where it posts."""
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-agree")
        exception, card, _ = exception_line(session, order)
        order_id, card_id = order.id, card.id

    with Session(db) as session:
        sync_cancel(session)

    client = TestClient(main.app)
    attention = client.get(PAGE).text
    order_page = client.get(f"/orders/{order_id}").text
    action = f'action="/inventory/{card_id}/un-remove/preview"'
    assert action in attention
    assert action in order_page


def test_no_add_back_while_the_card_is_not_removed(db):
    with Session(db) as session:
        open_order(session, external="mp-noaddback")
    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get(PAGE).text
    assert "mp-noaddback" in text
    # the phrase itself appears in the section's intro, so assert on the
    # actual control rather than the words
    assert "/un-remove/preview" not in text


# --- older audit rows still render --------------------------------------

def test_a_row_written_before_the_outcome_field_existed_still_reads(db):
    """Order 4117's real row predates v1.162.0 and has no "outcome" key.
    The fallback infers from the allocation it was released from -- in the
    renderer only; the stored row keeps exactly what was known then."""
    with Session(db) as session:
        order, _, card, _ = open_order(session, external="mp-legacy")
        card_id = card.id
    with Session(db) as session:
        sync_cancel(session)
    with Session(db) as session:
        record = session.query(OrderCancellation).one()
        detail = json.loads(record.released_cards_json)
        for entry in detail:
            entry.pop("outcome", None)
        record.released_cards_json = json.dumps(detail)
        session.commit()

    text = TestClient(main.app).get(PAGE).text
    assert "released to available" in text
    assert f"#{card_id}" in text


# --- the banner stays put -----------------------------------------------

def test_the_site_banner_is_unchanged_by_a_sync_cancellation(db):
    """Standing decision: the banner counts sync FAILURES only."""
    with Session(db) as session:
        open_order(session, external="mp-banner")
    with Session(db) as session:
        sync_cancel(session)

    text = TestClient(main.app).get("/inventory").text
    assert "failed to sync to Mana Pool" not in text
