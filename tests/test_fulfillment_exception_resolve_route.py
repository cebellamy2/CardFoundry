from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, FulfillmentException, InventoryCard, PickAllocation
from tests.test_fulfillment_exception_reconciliation import (
    remote_line, remote_order, submit_unresolved as submit,
)
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'resolve-route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_submitted_exception(session):
    order, item, card, allocation = seed(session)
    exception = submit(session, allocation)
    session.commit()
    return order, item, card, exception


def test_order_page_shows_resolve_button_for_submitted_awaiting_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, _, _, exception = make_submitted_exception(session)
        exception_id, order_id = exception.id, order.id

    client = TestClient(main.app)
    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert f'action="/fulfillment-exceptions/{exception_id}/resolve"' in page.text
    assert "Resolve" in page.text


def test_resolve_fetches_order_fresh_and_records_refunded_outcome(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, exception = make_submitted_exception(session)
        order_id, exception_id, item_id, card_id = order.id, exception.id, item.id, card.id

    with Session(db) as session:
        item = session.get(main.OrderItem, item_id)
        line = remote_line(item, "refunded")

    calls = []

    def fake_get_seller_order(order_id_arg):
        calls.append(order_id_arg)
        return {"order": remote_order(items=[line])}

    monkeypatch.setattr(main, "get_seller_order", fake_get_seller_order)

    client = TestClient(main.app)
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/resolve", follow_redirects=False,
    )
    # UX epic item 21: success now renders a confirmation page directly
    # (what changed, where to go next) instead of a silent redirect.
    assert response.status_code == 200
    assert "Fulfillment Exception Resolved" in response.text
    assert f'href="/orders/{order_id}"' in response.text
    assert calls == [order.external_order_id]

    with Session(db) as session:
        resolved = session.get(FulfillmentException, exception_id)
        assert resolved.remote_resolution_state == "resolved_refunded"
        assert resolved.remote_resolved_at is not None
        # Inventory resolution and card/allocation state are untouched --
        # this route only records the remote outcome.
        assert resolved.inventory_resolution_state == "unresolved"
        assert session.get(InventoryCard, card_id).status == "removed"


def test_resolve_records_review_required_when_order_has_multiple_pending_exceptions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item1, card1, allocation1 = seed(session)
        exception1 = submit(session, allocation1)
        _, item2, card2, allocation2 = seed(session)
        # Re-seed onto the SAME order by reusing order id via a second item.
        item2.order_id = order.id
        session.commit()
        exception2 = submit(session, allocation2)
        session.commit()
        order_id, exception1_id, exception2_id = order.id, exception1.id, exception2.id

    def fake_get_seller_order(order_id_arg):
        return {"order": remote_order(status="refunded")}

    monkeypatch.setattr(main, "get_seller_order", fake_get_seller_order)

    client = TestClient(main.app)
    response = client.post(
        f"/fulfillment-exceptions/{exception1_id}/resolve", follow_redirects=False,
    )
    assert response.status_code == 200

    with Session(db) as session:
        assert session.get(FulfillmentException, exception1_id).remote_resolution_state == "review_required"
        assert session.get(FulfillmentException, exception2_id).remote_resolution_state == "review_required"


def test_resolve_requires_submitted_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = seed(session)
        from fulfillment_exception_service import mark_fulfillment_exception
        exception = mark_fulfillment_exception(session, allocation.id, "missing", "note")
        session.commit()
        exception_id = exception.id

    client = TestClient(main.app)
    response = client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    assert response.status_code == 409
    assert "Not Ready to Resolve" in response.text
    assert "submit it before resolving" in response.text
    assert 'href="/orders/' in response.text


def test_resolve_rejects_already_resolved_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, exception = make_submitted_exception(session)
        line = remote_line(item, "refunded")
        exception_id = exception.id
        order_external_id = order.external_order_id

    def fake_get_seller_order(order_id_arg):
        return {"order": remote_order(items=[line])}

    monkeypatch.setattr(main, "get_seller_order", fake_get_seller_order)
    client = TestClient(main.app)
    first = client.post(f"/fulfillment-exceptions/{exception_id}/resolve", follow_redirects=False)
    assert first.status_code == 200

    second = client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    assert second.status_code == 409
    # UX epic item 21: distinguished from "still needs attention" via a
    # dedicated already-resolved template (info role, not warning/danger).
    assert "Already Resolved" in second.text
    assert 'href="/orders/' in second.text


def test_resolve_reports_mana_pool_fetch_failure_without_crashing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, exception = make_submitted_exception(session)
        exception_id = exception.id

    def failing_get_seller_order(order_id_arg):
        raise RuntimeError("Mana Pool request failed")

    monkeypatch.setattr(main, "get_seller_order", failing_get_seller_order)
    client = TestClient(main.app)
    response = client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    assert response.status_code == 502
    assert "Could not fetch the order from Mana Pool" in response.text

    with Session(db) as session:
        assert session.get(FulfillmentException, exception_id).remote_resolution_state == "awaiting"


def test_resolve_still_awaiting_when_mana_pool_has_no_outcome_yet(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, exception = make_submitted_exception(session)
        exception_id, order_id = exception.id, order.id

    def fake_get_seller_order(order_id_arg):
        return {"order": remote_order(status="processing")}

    monkeypatch.setattr(main, "get_seller_order", fake_get_seller_order)
    client = TestClient(main.app)
    response = client.post(f"/fulfillment-exceptions/{exception_id}/resolve", follow_redirects=False)
    assert response.status_code == 200

    with Session(db) as session:
        assert session.get(FulfillmentException, exception_id).remote_resolution_state == "awaiting"

    page = client.get(f"/orders/{order_id}")
    assert "Resolve" in page.text


# --- Ticket A (v1.156.0) ------------------------------------------------
# Two halves: the route must stop claiming success when nothing happened,
# and a terminal outcome must UNLOCK an explicit close-out rather than
# performing one.


def _fake_manapool(monkeypatch, db, item_id, status):
    """Build the mocked Mana Pool payload from a freshly-loaded OrderItem.

    remote_line() reads several attributes off the item, so it must run
    while the instance is still bound to a session -- the same pattern
    the older tests in this file already use."""
    with Session(db) as session:
        line = remote_line(session.get(main.OrderItem, item_id), status)
    monkeypatch.setattr(
        main, "get_seller_order",
        lambda external_id: {"order": remote_order(items=[line], status=status)},
    )


def test_resolve_reports_real_counts_instead_of_a_hardcoded_success(tmp_path, monkeypatch):
    """The exact production bug: a still-in-flight order produced
    {"resolved": 0, "ignored": 1} while the page said "Fulfillment
    Exception Resolved"."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, exception = make_submitted_exception(session)
        order_id, exception_id, item_id = order.id, exception.id, item.id

    _fake_manapool(monkeypatch, db, item_id, "processing")
    client = TestClient(main.app)
    response = client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    assert response.status_code == 200
    assert "Nothing To Resolve Yet" in response.text
    assert "Fulfillment Exception Resolved" not in response.text
    assert "Exceptions resolved" in response.text

    with Session(db) as session:
        assert session.get(FulfillmentException, exception_id).remote_resolution_state == "awaiting"


def test_resolve_reports_success_for_a_genuinely_delivered_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, _, exception = make_submitted_exception(session)
        exception_id, item_id = exception.id, item.id

    _fake_manapool(monkeypatch, db, item_id, "delivered")
    client = TestClient(main.app)
    response = client.post(f"/fulfillment-exceptions/{exception_id}/resolve")
    assert response.status_code == 200
    assert "Fulfillment Exception Resolved" in response.text

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.remote_resolution_state == "resolved_fulfilled"
        # decision #1: the inventory side is NOT auto-closed
        assert exception.inventory_resolution_state == "unresolved"


def test_a_terminal_outcome_no_longer_unlocks_anything_to_click(tmp_path, monkeypatch):
    """Replaces four close-out tests removed with the button itself
    (CF-AUTORESOLVE-001, 2026-09-21).

    Resolve's job is unchanged and still tested above: it records what
    Mana Pool said. What changed is that a terminal outcome used to
    reveal a "Close out inventory record" button, and that button is
    gone -- an exception now closes when the operator submits it, so the
    state the button needed cannot occur. Resolve must still not close
    the record by itself; that is Ticket A's narrower guarantee, which
    this change deliberately kept.
    """
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, exception = make_submitted_exception(session)
        order_id, exception_id, item_id, card_id = (
            order.id, exception.id, item.id, card.id,
        )

    _fake_manapool(monkeypatch, db, item_id, "delivered")
    client = TestClient(main.app)
    client.post(f"/fulfillment-exceptions/{exception_id}/resolve")

    after = client.get(f"/orders/{order_id}")
    assert "close-out-inventory" not in after.text
    assert "Close out inventory record" not in after.text

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.remote_resolution_state == "resolved_fulfilled"
        # the whole point: recording the outcome closed nothing
        assert exception.inventory_resolution_state == "unresolved"
        assert session.get(
            InventoryCard, card_id
        ).inventory_exception_state == "exception_unresolved"
