"""UX/design-system epic, item 23: final validation, QA, and usability
testing prep -- the epic's last item, run against the fully-assembled
result rather than any single page.

This file covers the three genuine defects the full-system print pass
found -- none visible from any single item's own tests, only from
Pick Wave Detail's markup interacting across items 12/15/22:

1. A real HTML-validity bug that predates this epic: the per-row
   "Remove order" <form> nested inside the page-level "Orders in
   Wave" ship <form> (forms cannot nest -- HTML parse-error recovery
   silently merged the *entire rest of the page*, including the whole
   Master Pick List, into the ship form). Invisible on screen (browsers
   still lay out the merged tree fine), but the ship form is
   class="no-print", so the actual physical picking artifact was
   printing blank. Fixed with the same form="id" cross-reference
   technique already used elsewhere in this file (bulk-toolbar
   checkboxes) -- confirmed via a real Chromium print-media DOM
   inspection, not assumed from the CSS.
2. Print-mode contrast: the dark-theme --cf-text/--cf-text-secondary
   tokens were never redefined for print, so headings and metadata
   rendered near-white on white. Fixing text alone then broke table
   headers the other way (near-black text on --cf-surface's near-black
   background) -- both text and surface neutral tokens needed
   resetting together for print, semantic badge colors left untouched
   since neither side of those pairs changes.
3. A closed <details> element does not lay out non-summary content at
   all internally, regardless of what CSS display a descendant is
   forced to -- item 15's own !important print override made the
   *table* render at full size but never made the <details> itself
   grow to contain it. Fixed with a beforeprint/afterprint listener
   that toggles the same `open` attribute the page's own "Expand all
   batches" button already uses, restored after printing.

Also covers a fourth finding from the item's own performance-review
checklist: a real query-count instrumentation pass (not guessed) found
/orders running one extra per-row COUNT query per order on the page
(5 orders -> 10 queries, 50 -> 55). Confirmed via git blame this
predates the whole epic (v0.0.7/v0.0.9, the original build) rather
than being a regression from any item here -- fixed anyway, using the
same one-aggregate-query-outside-the-loop technique item 14 already
established for the equivalent Pick Waves List problem.

Route-level regression coverage for pick-wave-detail's existing
behavior lives in test_pick_wave_detail_item15_redesign.py and
test_pick_wave_routes.py; this file only covers the item 23 fixes.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'qa_item23.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_wave_with_order(session):
    wave = PickWave(label="Wave 1", status="active")
    session.add(wave)
    session.flush()
    order = SalesOrder(
        external_order_id="MP-9001", source="manapool", status="in_pick_wave",
        shipping_name="Customer Test", shipping_line1="100 Main St",
        shipping_city="Austin", shipping_state="TX", shipping_postal_code="78701",
    )
    session.add(order)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    batch = Batch(batch_code="A1")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
        finish_id="NF", condition_id="LP", status="reserved",
    )
    session.add(card)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, quantity=1)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status="allocated",
    ))
    session.commit()
    return wave, order


# --- fix 1: remove-order form is no longer nested inside the ship form --

def test_remove_order_form_is_not_nested_inside_the_ship_form(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order = make_wave_with_order(session)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text

    ship_form_start = html.index('action="/pick-waves/')
    ship_form_start = html.index(f'action="/pick-waves/{wave_id}/ship"')
    ship_form_close = html.index("</form>", ship_form_start)
    between = html[ship_form_start:ship_form_close]
    assert "<form" not in between


def test_remove_order_button_is_wired_via_form_attribute(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order = make_wave_with_order(session)
        wave_id, order_id = wave.id, order.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text

    remove_form_id = f"remove-order-{order_id}"
    assert f'<button type="submit" form="{remove_form_id}">' in html
    assert (
        f'<form\n                    id="{remove_form_id}"\n'
        '                    class="no-print"'
    ) in html
    assert f'action="/pick-waves/{wave_id}/orders/{order_id}/remove"' in html


def test_master_pick_list_still_renders_after_the_form_fix(tmp_path, monkeypatch):
    """Regression guard for the fix itself -- the pick list content must
    still actually be present in the DOM (not just structurally
    unnested)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order = make_wave_with_order(session)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert 'class="pick-batch section-disclosure" id="batch-A1"' in html
    assert "Lightning Bolt" in html


# --- fix 2: print-mode token overrides ----------------------------------

def test_print_media_resets_neutral_text_and_surface_tokens(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order = make_wave_with_order(session)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "@media print {" in html
    print_block = html[html.index("@media print {"):]
    print_block = print_block[:print_block.index(":root {") + 400]
    assert "--cf-text: #000000;" in print_block
    assert "--cf-text-secondary: #1a1a1a;" in print_block
    assert "--cf-text-muted: #444444;" in print_block
    assert "--cf-surface: #ffffff;" in print_block
    assert "--cf-surface-elevated: #ffffff;" in print_block


# --- fix 3: beforeprint/afterprint force-opens pick-batch sections -----

def test_beforeprint_listener_forces_pick_batches_open(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave, order = make_wave_with_order(session)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "addEventListener('beforeprint'" in html
    assert "addEventListener('afterprint'" in html
    assert "details.pick-batch:not([open])" in html
    assert "d.open = true;" in html
    assert "d.open = false;" in html


def test_no_beforeprint_listener_when_there_are_no_batches(tmp_path, monkeypatch):
    """batch_toolbar_html (which the script lives in) is only rendered
    when grouped is truthy -- an empty wave shouldn't get dead script."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        wave = PickWave(label="Empty Wave", status="active")
        session.add(wave)
        session.commit()
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "addEventListener('beforeprint'" not in html


# --- shipment-sync banner no longer prints ------------------------------

def test_shipment_sync_banner_is_wrapped_no_print(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="stuck-1", source="manapool", status="shipped",
        ))
        session.commit()
    html = TestClient(main.app).get("/inventory").text
    assert "failed to sync to Mana Pool" in html
    banner_start = html.index('<div class="no-print">')
    banner_region = html[banner_start:banner_start + 300]
    assert "failed to sync to Mana Pool" in banner_region


# --- fix 4: /orders per-row item-count N+1 -------------------------------
#
# 2026-08-30: this column now shows total physical cards (SUM of
# OrderItem.quantity), not line count (COUNT of order_item rows) -- see
# "total card count" epic. Same aggregate query, same N+1 guard; the
# rendered value assertion below was updated from a line-count number to
# a card-count number (they happened to coincide before this change,
# since every fixture line had quantity=1 -- test_orders_page_shows_
# total_cards_not_line_count below is the one that actually
# distinguishes the two).

def test_orders_page_does_not_run_a_per_row_card_count_query(tmp_path, monkeypatch):
    from models import OrderItem

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for i in range(20):
            order = SalesOrder(
                external_order_id=f"MP-{i}", source="manapool", status="ready_to_pick",
            )
            session.add(order)
            session.flush()
            session.add(OrderItem(order_id=order.id, name="Lightning Bolt", quantity=1))
        session.commit()

    queries = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db, "before_cursor_execute", before_cursor_execute)
    try:
        resp = TestClient(main.app).get("/orders")
    finally:
        event.remove(db, "before_cursor_execute", before_cursor_execute)

    assert resp.status_code == 200
    card_count_queries = [q for q in queries if "order_items" in q and "WHERE" in q]
    # One aggregate GROUP BY query for the whole page, not one per order.
    assert len(card_count_queries) <= 1, (
        f"expected at most 1 aggregate card-count query for 20 orders, "
        f"got {len(card_count_queries)} -- looks like the per-row N+1 is back"
    )


def test_orders_page_still_shows_correct_card_counts(tmp_path, monkeypatch):
    from models import OrderItem

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="MP-1", source="manapool", status="ready_to_pick",
        )
        session.add(order)
        session.flush()
        session.add(OrderItem(order_id=order.id, name="Lightning Bolt", quantity=1))
        session.add(OrderItem(order_id=order.id, name="Sol Ring", quantity=1))
        session.add(OrderItem(order_id=order.id, name="Counterspell", quantity=1))
        session.commit()

    html = TestClient(main.app).get("/orders").text
    assert "<td>\n                    3\n                </td>" in html


def test_orders_page_shows_total_cards_not_line_count(tmp_path, monkeypatch):
    """The case the whole slice is about: a line with quantity > 1 must
    make the Cards column diverge from a plain line count. 2 lines, one
    of them qty 3, shows 4 -- not 2."""
    from models import OrderItem

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="MP-1", source="manapool", status="ready_to_pick",
        )
        session.add(order)
        session.flush()
        session.add(OrderItem(order_id=order.id, name="Lightning Bolt", quantity=3))
        session.add(OrderItem(order_id=order.id, name="Sol Ring", quantity=1))
        session.commit()

    html = TestClient(main.app).get("/orders").text
    assert "<th>Cards</th>" in html
    assert "<th>Lines</th>" not in html
    assert "<td>\n                    4\n                </td>" in html
