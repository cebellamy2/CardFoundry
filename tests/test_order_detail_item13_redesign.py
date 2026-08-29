"""UX/design-system epic, item 13: Order Detail and Allocation
Troubleshooting redesign.

Verified current state first, per the item's own instruction: order
cancellation's confirmation (naming the exact order and card count) was
already correct and is untouched here -- see tests/test_order_cancel_confirmation.py,
all of which still pass unchanged. Items 6-8 had NOT yet touched this page's
own Mana Pool Status text or picklist finish/status values (unlike the
Orders list, item 12) -- confirmed via investigation before writing any code.

This file covers what was newly built: the Mana Pool Status badge (reusing
item 12's filled/outlined convention), the structured <dl> summary card,
the shipping-address disclosure (Section 19 privacy), the consolidated
per-row exception-report disclosure (mirroring an existing pattern already
shipped on the Master Pick List page), progressive disclosure for the
picklist, normalized finish/status labels in the picklist, and two real
state-vs-display bugs found via live verification: a stale "everything
was found" success banner after a later exception, and "Mark Packed"/
"Mark Shipped" being offered even when the backend would already refuse
the transition.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'order-detail-item13.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_order_with_allocation(
    session, *, status="ready_to_pick", remote_status=None, allocation_status="allocated",
    external_order_id="mp-1", external_label=None, quantity=1,
    shipping_name=None, shipping_line1=None,
):
    batch = Batch(batch_code=f"B-{external_order_id}")
    session.add(batch)
    session.flush()
    order = SalesOrder(
        external_order_id=external_order_id, external_label=external_label,
        source="manapool", status=status, remote_fulfillment_status=remote_status,
        shipping_name=shipping_name, shipping_line1=shipping_line1,
    )
    session.add(order)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
        scryfall_id="sf-bolt", mtgjson_id="mtg-bolt", language_id="EN",
        condition_id="LP", finish_id="NF", finish="NF", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id, quantity=quantity,
    )
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status=allocation_status,
    )
    session.add(allocation)
    session.commit()
    return order, item, card, allocation


# --- Mana Pool Status badge (reusing item 12's convention) --------------

def test_mana_pool_status_renders_as_outlined_remote_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, remote_status="processing")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'class="badge badge-info badge-remote"' in response.text
    assert "Processing" in response.text


def test_mana_pool_status_blank_shows_not_synced_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, remote_status=None)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'class="badge badge-neutral badge-remote"' in response.text
    assert "Not Synced" in response.text


def test_cardfoundry_status_stays_filled_not_outlined(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert 'class="badge badge-info">' in response.text


# --- structured summary card ---------------------------------------------

def test_summary_card_is_a_real_definition_list(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert '<dl class="order-summary-card">' in response.text
    assert "<dt>Source</dt>" in response.text
    assert "<dt>CardFoundry Status</dt>" in response.text
    assert "<dt>Mana Pool Status</dt>" in response.text


def test_summary_card_omits_timestamps_that_are_not_set(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert "<dt>Picked</dt>" not in response.text
    assert "<dt>Packed</dt>" not in response.text
    assert "<dt>Shipped</dt>" not in response.text
    assert "<dt>Tracking</dt>" not in response.text


def test_summary_card_shows_tracking_once_shipped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="shipped", allocation_status="shipped")
        order.tracking_number = "1Z999AA10123456784"
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert "<dt>Tracking</dt>" in response.text
    assert "1Z999AA10123456784" in response.text


# --- page header / breadcrumbs --------------------------------------------

def test_page_uses_page_header_with_breadcrumbs(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, external_order_id="mp-77")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert '<header class="page-header">' in response.text
    assert '<a href="/orders">Orders</a>' in response.text
    assert 'href="/orders/{}/packing-slip" target="_blank" class="btn-secondary'.format(order_id) in response.text


# --- shipping address: collapsed disclosure, Section 19 privacy ---------

def test_shipping_address_is_a_collapsed_disclosure(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(
            session, shipping_name="Jane Doe", shipping_line1="123 Main St",
        )
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert '<details class="section-disclosure no-print">' in response.text
    assert "<summary>Shipping Address</summary>" in response.text
    # The address text is still server-rendered (collapsed, not removed) --
    # a real user can open it, and this stays testable via string presence.
    assert "Jane Doe" in response.text
    assert "Copy Address" in response.text


def test_shipping_address_disclosure_omitted_entirely_when_no_address(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert "Shipping Address" not in response.text
    assert "Copy Address" not in response.text


# --- consolidated exception-report control --------------------------------

def test_report_exception_control_is_a_collapsed_disclosure(tmp_path, monkeypatch):
    """Mirrors the pattern already shipped on the Master Pick List page --
    order_detail was the one place still showing this always-expanded."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "<summary>Report Exception</summary>" in response.text
    trigger_index = response.text.index("<summary>Report Exception</summary>")
    details_start = response.text.rindex("<details>", 0, trigger_index)
    form_index = response.text.index("<form", trigger_index)
    close_index = response.text.index("</details>", form_index)
    assert details_start < trigger_index < form_index < close_index


# --- normalized labels in the picklist (item 6 layer) ---------------------

def test_picklist_finish_and_status_are_normalized_not_raw(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, allocation_status="allocated")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'class="badge badge-neutral">' in response.text  # allocated -> neutral badge
    assert "Allocated" in response.text
    assert "Non-Foil" in response.text  # _finish_display("normal")


# --- progressive disclosure: picklist batch default open/closed ---------

def test_picklist_batch_closed_by_default_for_a_clean_ready_to_pick_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    batch_start = response.text.index('<details class="pick-batch section-disclosure">')
    assert response.text[batch_start:batch_start + 60].count(" open") == 0


def test_picklist_batch_open_by_default_for_a_short_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="short", quantity=2,
        )
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert '<details class="pick-batch section-disclosure" open>' in response.text


def test_picklist_batch_summary_shows_card_count(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session)
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert "Batch B-mp-1 &mdash; 1 card(s)" in response.text


# --- data-table-scroll containment on all three tables --------------------

def test_all_three_tables_are_wrapped_in_scroll_containers(tmp_path, monkeypatch):
    """Needs one *still-allocated* card alongside the excepted one --
    marking a card exceptional moves its allocation to status="exception",
    which get_picklist() excludes, so a single-card order's picklist table
    would otherwise disappear entirely (a real, separate, correct
    behavior, not something to work around by weakening this test)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(session)
        batch = session.get(Batch, card.batch_id)
        second_card = InventoryCard(
            batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="2",
            scryfall_id="sf-bolt-2", mtgjson_id="mtg-bolt", language_id="EN",
            condition_id="LP", finish_id="NF", finish="NF", status="reserved",
        )
        session.add(second_card)
        session.flush()
        second_item = OrderItem(
            order_id=order.id, name=second_card.name, set_code=second_card.set_code,
            collector_number=second_card.collector_number, scryfall_id=second_card.scryfall_id,
            mtgjson_id=second_card.mtgjson_id, language_id=second_card.language_id,
            condition_id=second_card.condition_id, finish_id=second_card.finish_id, quantity=1,
        )
        session.add(second_item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=second_item.id, inventory_card_id=second_card.id,
            batch_id=batch.id, status="allocated",
        ))
        session.commit()
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert response.text.count('<div class="data-table-scroll">') == 3
    assert '<table class="data-table density-compact">' in response.text


# --- found live: stale "fully allocated" banner after a later exception -

def test_ready_to_pick_success_banner_reflects_live_totals_not_stale_status(tmp_path, monkeypatch):
    """A real bug found via live verification: order.status is set once
    at allocation time and never revisited. Reporting a "missing"
    exception against an already-allocated line later leaves
    order.status at ready_to_pick while the order is no longer actually
    fully allocated -- the banner must reflect the live totals this page
    already computes, not just order.status."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(session, status="ready_to_pick")
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "Every requested card was" not in response.text
    assert "fully allocated, but a fulfillment" in response.text
    assert "exception reported since then means" in response.text
    assert "<strong>0</strong>" in response.text and "<strong>1</strong>" in response.text


def test_ready_to_pick_success_banner_unchanged_for_a_genuinely_clean_order(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="ready_to_pick")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert "Every requested card was" in response.text
    assert "found and reserved" in response.text


# --- found live: Mark Packed/Shipped offered even when blocked ----------

def test_mark_packed_hidden_when_a_submission_blocking_exception_exists(tmp_path, monkeypatch):
    """Real bug: mark_packed() already refuses this transition (see
    order_has_fulfillment_submission_block), but the page offered the
    button regardless -- clicking it hit an unhandled error. Reuses the
    same existing invariant function the backend already relies on."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="picked", allocation_status="picked",
        )
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert 'action="/orders/{}/packed"'.format(order_id) not in response.text
    assert "exception awaiting Mana" in response.text
    assert "before marking this order packed" in response.text


def test_mark_packed_still_shown_when_no_blocking_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="picked", allocation_status="picked")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert f'action="/orders/{order_id}/packed"' in response.text
    assert "Mark Packed" in response.text


def test_mark_shipped_hidden_when_a_submission_blocking_exception_exists(tmp_path, monkeypatch):
    """mark_packed() itself would already refuse to reach order.status ==
    "packed" while an allocation still needs_submission (ALLOWED_ALLOCATION_STATUSES
    excludes "packed" entirely, so a real exception can't be *reported*
    at that stage either) -- this defensive display fix is for the same
    data shape regardless of how it might arise. Reported while the
    allocation was still "picked" (a genuinely allowed state), then
    advanced directly in the DB to represent the case being defended
    against."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="picked", allocation_status="picked",
        )
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order.status = "packed"
        allocation.status = "packed"
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/shipped"' not in response.text
    assert "before marking this order shipped" in response.text


def test_mark_shipped_still_shown_when_no_blocking_exception(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, *_ = make_order_with_allocation(session, status="packed", allocation_status="packed")
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert f'action="/orders/{order_id}/shipped"' in response.text
    assert "Mark Shipped" in response.text


def test_submission_block_does_not_affect_cancellation(tmp_path, monkeypatch):
    """cancel_order()/release_order() have no such guard -- confirm the
    gating logic added here doesn't accidentally spread to a genuinely
    unrelated action."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order, item, card, allocation = make_order_with_allocation(
            session, status="ready_to_pick", allocation_status="allocated",
        )
        mark_fulfillment_exception(session, allocation.id, "missing", "Not found")
        session.commit()
        order_id = order.id
    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert f'action="/orders/{order_id}/cancel"' in response.text
