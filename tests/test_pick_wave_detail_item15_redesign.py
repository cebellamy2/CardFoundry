"""UX/design-system epic, item 15: Pick Wave Detail redesign.

Verified current state first, per the item's own instruction: overflow
containment (item 4, re-confirmed by the site-wide sweep in v1.84.1),
status badges (item 6), and the confirmation-message pattern (Phase 2)
were already live on this page. This file covers what was newly built:
code-prefix batch-code grouping (measured live against production
before writing the grouping logic -- see _batch_code_group), a sticky
wave-summary header with new batch-count/exception-count/progress
fields, wave-level vs. order-level vs. card-level action separation
(including the first real usage of .btn-destructive anywhere in the
app), collapsible+grouped batch sections with an expand/collapse-all
toolbar and an in-page batch index, per-batch progress computed from
already-loaded allocation data (no new query), row-level Copy
Address/Remove consolidation into the same bare <details> mechanism
this page already used for per-card exception reporting, a duplicate-
<h1> fix, and the details:not([open]) shadow-DOM overflow-residual fix
the site-wide sweep flagged and deliberately left for this item.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from main import _batch_code_group
from models import (
    Base, Batch, FulfillmentException, InventoryCard, OrderItem,
    PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pick-wave-detail-item15.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def _batch(session, batch_code):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code)
        session.add(batch)
        session.flush()
    return batch


def make_wave(session, *, label="Wave 1", wave_status="active"):
    wave = PickWave(label=label, status=wave_status)
    session.add(wave)
    session.commit()
    return wave


def add_order_with_card(
    session, wave, *, batch_code="A1", allocation_status="allocated",
    membership_status="active", with_exception=False, shipping=True,
):
    order = SalesOrder(
        external_order_id=f"o-{wave.id}-{batch_code}-{allocation_status}-{session.query(SalesOrder).count()}",
        source="manapool", status="in_pick_wave",
        shipping_name="Customer Test" if shipping else None,
        shipping_line1="100 Main St" if shipping else None,
        shipping_city="Austin" if shipping else None,
        shipping_state="TX" if shipping else None,
        shipping_postal_code="78701" if shipping else None,
    )
    session.add(order)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status=membership_status))
    batch = _batch(session, batch_code)
    card = InventoryCard(
        batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
        finish_id="NF", condition_id="LP", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1)
    session.add(item)
    session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status=allocation_status,
    )
    session.add(allocation)
    session.flush()
    if with_exception:
        session.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=item.id, pick_allocation_id=allocation.id,
            inventory_card_id=card.id, exception_type="missing",
            submission_state="needs_submission", remote_resolution_state="awaiting",
            inventory_resolution_state="unresolved", note="Missing at pick.",
        ))
    session.commit()
    return order, allocation


# --- _batch_code_group: the real production-measured grouping logic ------

def test_plain_operator_named_batches_are_ungrouped():
    assert _batch_code_group("A7") == ("", "")
    assert _batch_code_group("A20") == ("", "")
    assert _batch_code_group("z3") == ("", "")


def test_leg_prefix_groups_as_legacy_import_batches():
    assert _batch_code_group("leg_b") == ("LEG", "Legacy Import Batches")
    assert _batch_code_group("leg_foil_c") == ("LEG", "Legacy Import Batches")
    assert _batch_code_group("LEG_X") == ("LEG", "Legacy Import Batches")


def test_con_prefix_groups_as_consignment_batches():
    assert _batch_code_group("CON_AID") == ("CON", "Consignment Batches")
    assert _batch_code_group("CON_CAM_ROC") == ("CON", "Consignment Batches")


def test_unanticipated_prefix_gets_a_readable_fallback_label():
    # Zero examples of this existed in production when measured live,
    # but the grouping logic must not assume the set is closed.
    assert _batch_code_group("PROMO_2026") == ("PROMO", "PROMO Batches")


# --- batch sections: grouped, collapsible, indexed ------------------------

def test_batches_grouped_into_sections_by_code_prefix(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        add_order_with_card(session, wave, batch_code="CON_AID")
        add_order_with_card(session, wave, batch_code="leg_b")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert '<section class="pick-batch-group">' in response.text
    assert "<h2>Consignment Batches</h2>" in response.text
    assert "<h2>Legacy Import Batches</h2>" in response.text
    # Plain batch A1 stays ungrouped -- not wrapped in a labeled section.
    # (The batch index at the top lists every group label before any
    # section content, so anchor on the actual <h2> section heading.)
    assert response.text.index("Batch A1") < response.text.index("<h2>Consignment Batches</h2>")


def test_batch_sections_are_open_details_by_default(tmp_path, monkeypatch):
    """Changed from closed-by-default (item 15's original design) to
    open-by-default per direct operator request -- the whole Master Pick
    List is meant to be read at a glance while standing at a shelf, not
    expanded batch-by-batch first. "Collapse all batches" (still present)
    is how an operator opts into the denser view instead."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert '<details class="pick-batch section-disclosure" id="batch-A1" open>' in response.text


def test_master_pick_list_section_comes_before_orders_in_wave(tmp_path, monkeypatch):
    """Changed per direct operator request -- the actual physical picking
    artifact leads the page; "Orders in Wave" (the operational/shipping
    table) now follows it, not the other way around."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    master_pick_list_idx = response.text.index("<h2>\n            Master Pick List")
    orders_in_wave_idx = response.text.index('<h2 class="no-print">\n            Orders in Wave')
    assert master_pick_list_idx < orders_in_wave_idx


def test_batch_index_links_every_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        add_order_with_card(session, wave, batch_code="A2")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert '<nav class="batch-index no-print" aria-label="Batch sections">' in response.text
    assert '<a href="#batch-A1">A1</a>' in response.text
    assert '<a href="#batch-A2">A2</a>' in response.text


def test_expand_collapse_all_toolbar_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "Expand all batches" in response.text
    assert "Collapse all batches" in response.text


def test_per_batch_progress_shown_without_extra_query(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", allocation_status="picked")
        add_order_with_card(session, wave, batch_code="A1", allocation_status="allocated")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "2 card(s), 1/2 picked" in response.text


def test_no_batches_means_no_toolbar_or_index(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_wave(session)
    response = TestClient(main.app).get("/pick-waves/1")
    assert "Expand all batches" not in response.text
    assert "No cards are currently assigned to this wave." in response.text


# --- sticky wave-summary header: new fields --------------------------------

def test_wave_summary_includes_batch_and_exception_counts(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", with_exception=True)
        add_order_with_card(session, wave, batch_code="A2")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert 'class="wave-summary wave-summary-sticky"' in response.text
    assert "Batches:" in response.text
    assert "<strong>2</strong>" in response.text  # batch count
    assert 'class="wave-summary-exception-link"' in response.text
    assert "1 ⚠" in response.text
    assert "Progress:" in response.text
    assert "0/2 picked" in response.text


def test_wave_summary_shows_zero_exceptions_plainly(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "<strong>0</strong>" in response.text
    assert 'class="wave-summary-exception-link"' not in response.text


# --- wave-level action hierarchy: primary/secondary/destructive -----------

def test_cancel_wave_uses_the_shared_destructive_button_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert (
        f'action="/pick-waves/{wave_id}/cancel"' in response.text
    )
    cancel_button_idx = response.text.index(f'/pick-waves/{wave_id}/cancel')
    snippet = response.text[cancel_button_idx:cancel_button_idx + 300]
    assert 'class="btn-destructive"' in snippet


def test_complete_wave_is_primary_when_no_unresolved_exceptions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    complete_idx = response.text.index(f'/pick-waves/{wave_id}/complete')
    snippet = response.text[complete_idx:complete_idx + 600]
    assert 'class="btn-primary"' in snippet
    assert "have an unresolved" not in response.text


def test_complete_wave_is_secondary_with_unresolved_exceptions(tmp_path, monkeypatch):
    # complete_pick_wave() has no hard blocking precondition -- it always
    # succeeds and gracefully skips exception-blocked orders. The soft
    # de-emphasis below must reflect that real, non-blocking behavior.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", with_exception=True)
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    complete_idx = response.text.index(f'/pick-waves/{wave_id}/complete')
    snippet = response.text[complete_idx:complete_idx + 600]
    assert 'class="btn-secondary"' in snippet
    assert "1 order(s) have an unresolved" in response.text
    assert 'href="#fulfillment-exceptions"' in response.text


def test_submitted_exception_does_not_count_as_unresolved(tmp_path, monkeypatch):
    # Only submission_state == "needs_submission" actually blocks
    # complete_pick_wave() from advancing an order (fulfillment_exception_
    # invariants.exception_blocks_order_completion) -- a submitted
    # exception must not still read as "unresolved" here.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        _, allocation = add_order_with_card(session, wave, batch_code="A1", with_exception=True)
        exception = session.query(FulfillmentException).filter_by(
            pick_allocation_id=allocation.id
        ).one()
        exception.submission_state = "submitted"
        session.commit()
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    complete_idx = response.text.index(f'/pick-waves/{wave_id}/complete')
    snippet = response.text[complete_idx:complete_idx + 600]
    assert 'class="btn-primary"' in snippet


def test_reopen_uses_primary_button_class(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, wave_status="completed")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    reopen_idx = response.text.index(f'/pick-waves/{wave_id}/reopen')
    snippet = response.text[reopen_idx:reopen_idx + 600]
    assert 'class="btn-primary"' in snippet


# --- print artifacts: confirmed exactly two exist, gated correctly --------

def test_print_master_pick_list_available_when_active(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, wave_status="active")
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.text.count('onclick="window.print()"') == 1
    assert 'class="btn-secondary" onclick="window.print()">' in response.text


def test_print_master_pick_list_unavailable_when_completed(tmp_path, monkeypatch):
    # get_wave_picklist() itself returns empty for a completed wave
    # (memberships close on completion) -- confirmed pre-existing,
    # intentional behavior, preserved here, now explained instead of
    # silently absent.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, wave_status="completed")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert 'onclick="window.print()"' not in response.text
    assert "only available while this" in response.text
    assert "already empty here" in response.text


def test_print_all_packing_slips_always_available(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, wave_status="cancelled")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert f'href="/pick-waves/{wave_id}/packing-slips"' in response.text


# --- order-row action consolidation (Copy Address, Remove) ----------------

def test_order_row_actions_consolidated_into_one_disclosure(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", shipping=True)
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.text.count("<summary>Actions</summary>") == 1
    actions_idx = response.text.index("<summary>Actions</summary>")
    snippet = response.text[actions_idx:actions_idx + 1200]
    assert "Copy Address" in snippet
    assert "Remove" in snippet
    # Old separate "Shipping Address" table column is gone.
    assert "<th>Shipping Address</th>" not in response.text
    assert "<th>Actions</th>" in response.text


def test_no_actions_disclosure_when_nothing_to_show(tmp_path, monkeypatch):
    # A completed wave has no Remove action and, without a shipping
    # address on the seeded order, nothing to disclose either.
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, wave_status="completed")
        add_order_with_card(session, wave, batch_code="A1", shipping=False, membership_status="closed")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "<summary>Actions</summary>" not in response.text


# --- heading hierarchy: one real <h1> --------------------------------------

def test_exactly_one_h1_on_the_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.text.count("<h1") == 1
    assert "Master Pick List" in response.text
    assert "<h2>\n            Master Pick List\n        </h2>" in response.text


def test_page_header_has_breadcrumbs(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session, label="Wave A")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in response.text
    assert '<a href="/pick-waves">Pick Waves</a>' in response.text
    assert '<span class="breadcrumb-current">Wave A</span>' in response.text


# --- the shadow-DOM overflow-residual fix ----------------------------------

def test_closed_details_children_forced_to_zero_geometry_css_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert "details:not([open]) > *:not(summary) {" in response.text
    assert "display: none;" in response.text


def test_print_media_force_shows_pick_batch_content_css_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert ".pick-batch:not([open]) > *:not(summary) {" in response.text
    assert "display: block !important;" in response.text


# --- no functional regression: state-changing routes untouched ------------

def test_complete_wave_route_still_functions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", allocation_status="picked")
        wave_id = wave.id
    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code in (200, 302, 303)
    with Session(db) as session:
        wave = session.get(PickWave, wave_id)
        assert wave.status == "completed"


def test_cancel_wave_route_still_functions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    client = TestClient(main.app)
    response = client.post(f"/pick-waves/{wave_id}/cancel", follow_redirects=False)
    assert response.status_code in (200, 302, 303)
    with Session(db) as session:
        wave = session.get(PickWave, wave_id)
        assert wave.status == "cancelled"


def test_remove_order_route_still_functions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        order, _ = add_order_with_card(session, wave, batch_code="A1")
        wave_id, order_id = wave.id, order.id
    client = TestClient(main.app)
    response = client.post(
        f"/pick-waves/{wave_id}/orders/{order_id}/remove", follow_redirects=False,
    )
    assert response.status_code in (200, 302, 303)
    with Session(db) as session:
        membership = session.query(PickWaveOrder).filter_by(
            wave_id=wave_id, order_id=order_id,
        ).one()
        assert membership.status == "removed"


def test_wave_not_found_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pick-waves/999")
    assert response.status_code == 404


# --- Master Pick List Condition column (2026-08-31) ------------------------

def test_master_pick_list_shows_the_card_condition(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1")
        wave_id = wave.id
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    assert "<th>Condition</th>" in response.text
    assert "<td>Lightly Played</td>" in response.text


# --- "Orders in Wave" Cards column (2026-08-30, total card count epic) ----

def test_orders_in_wave_shows_total_cards_not_line_count(tmp_path, monkeypatch):
    """The case the whole slice is about: 2 lines, one of them qty 3,
    shows 4 in the Orders in Wave table -- not a count of order_item rows,
    and not the wave-wide total_cards shown separately in the sticky
    summary above."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        order, allocation = add_order_with_card(session, wave, batch_code="A1")
        card = session.get(InventoryCard, allocation.inventory_card_id)
        item = session.get(OrderItem, allocation.order_item_id)
        item.quantity = 3
        second_card = InventoryCard(
            batch_id=card.batch_id, name="Sol Ring", set_code="LEA", collector_number="2",
            finish_id="NF", condition_id="LP", status="reserved",
        )
        session.add(second_card)
        session.flush()
        second_item = OrderItem(order_id=order.id, name=second_card.name, quantity=1)
        session.add(second_item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=second_item.id, inventory_card_id=second_card.id,
            batch_id=second_card.batch_id, status="allocated",
        ))
        session.commit()
        wave_id = wave.id

    response = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    assert response.status_code == 200
    orders_in_wave_idx = response.text.index('<h2 class="no-print">\n            Orders in Wave')
    table_region = response.text[orders_in_wave_idx:orders_in_wave_idx + 2000]
    assert "<th>Cards</th>" in table_region
    assert "<td>4</td>" in table_region


def test_orders_in_wave_cards_column_does_not_add_a_per_order_query(tmp_path, monkeypatch):
    """Real query-count instrumentation, same technique and bar as item
    23's /orders fix: one aggregate GROUP BY query for the whole page,
    not one per order in the Orders in Wave table."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = make_wave(session)
        for i in range(15):
            add_order_with_card(session, wave, batch_code=f"A{i}")
        wave_id = wave.id

    queries = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db, "before_cursor_execute", before_cursor_execute)
    try:
        resp = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    finally:
        event.remove(db, "before_cursor_execute", before_cursor_execute)

    assert resp.status_code == 200
    # Match this specific new aggregate, not every query that happens to
    # touch order_items (get_wave_picklist's own join included) --
    # sum(quantity) is unique to the Cards-column query added here.
    card_count_queries = [q for q in queries if "sum(order_items.quantity)" in q.lower()]
    assert len(card_count_queries) <= 1, (
        f"expected at most 1 aggregate card-count query for 15 orders, "
        f"got {len(card_count_queries)} -- looks like a new per-row N+1"
    )
