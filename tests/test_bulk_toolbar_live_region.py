"""Accessibility follow-up to the item 22 audit (v1.93.0, 0c222f6):
the shared bulk-selection toolbar's "N selected" count is CSS
`::before` content (see .bulk-toolbar-count / counter-increment in
test_phase2_shared_table_toolbar.py), which never enters the
accessibility tree -- Section 14 requires selection state be announced
to assistive technology. Operator-approved 2026-08-30.

_bulk_toolbar_live_region_script() is the one piece of JS involved: on
any checkbox `change` event, it recomputes the same count the CSS
counter already displays (identical selector, scoped to the same
.table-wrap) and mirrors it into a visually-hidden aria-live region.
It does not drive the toolbar's own show/hide or visible count --
:has() and CSS counters still do that untouched, exactly as covered in
test_phase2_shared_table_toolbar.py's own tests.

Covers all three surfaces the shared toolbar/live-region appear on:
Inventory Search and /batches/{id} (both via _bulk_card_action_form,
the bulk-toolbar-any counter), and Orders (both its wave and pack
toolbars, one script call covering both).
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'bulk_toolbar_live_region.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, batch):
    card = InventoryCard(
        batch_id=batch.id, name="Lightning Bolt", set_code="LEA", collector_number="1",
        finish_id="NF", condition_id="NM", language_id="EN", status="available",
    )
    session.add(card)
    session.commit()
    return card


# --- sr-only utility CSS --------------------------------------------------

def test_sr_only_utility_class_is_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".sr-only {" in html
    rule = html[html.index(".sr-only {"):]
    rule = rule[:rule.index("}") + 1]
    assert "clip: rect(0, 0, 0, 0);" in rule


# --- Inventory Search (bulk-toolbar-any, via _bulk_card_action_form) -----

def test_inventory_search_toolbar_has_a_live_region(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert (
        '<span class="bulk-toolbar-count-live sr-only" '
        'aria-live="polite" aria-atomic="true"></span>'
    ) in html
    # It must sit inside the bulk-toolbar-any form, not floating loose.
    form_start = html.index('class="bulk-toolbar bulk-toolbar-any')
    form_end = html.index("</form>", form_start)
    assert "bulk-toolbar-count-live" in html[form_start:form_end]
    assert "<script>" in html[form_start:]


# --- /batches/{id} (bulk-toolbar-any, same shared component) -----------

def test_batch_detail_toolbar_has_a_live_region(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.commit()
        batch_id = batch.id
    html = TestClient(main.app).get(f"/batches/{batch_id}").text
    assert (
        '<span class="bulk-toolbar-count-live sr-only" '
        'aria-live="polite" aria-atomic="true"></span>'
    ) in html
    assert "<script>" in html


# --- Orders (both toolbars, one shared script) ---------------------------

def test_orders_wave_toolbar_has_a_live_region(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    form_start = html.index('class="bulk-toolbar bulk-toolbar-wave')
    form_end = html.index("</form>", form_start)
    assert "bulk-toolbar-count-live" in html[form_start:form_end]


def test_orders_pack_toolbar_has_a_live_region(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    form_start = html.index('class="bulk-toolbar bulk-toolbar-pack')
    form_end = html.index("</form>", form_start)
    assert "bulk-toolbar-count-live" in html[form_start:form_end]


def test_orders_page_gets_exactly_one_live_region_script(tmp_path, monkeypatch):
    """Two toolbars share one script -- not one copy each."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    assert html.count("addEventListener('change'") == 1


# --- the script's own logic ----------------------------------------------

def test_live_region_script_covers_all_three_selection_kinds(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    script = html[html.index("<script>"):html.index("</script>") + len("</script>")]
    assert 'any: \'input[type="checkbox"]\',' in script
    assert 'wave: \'input[name="order_ids"]\',' in script
    assert 'pack: \'input[name="pack_order_ids"]\',' in script
    # Reads from the toolbar's own class, same distinction the CSS
    # ::before rules use -- not a separate, potentially-drifting list.
    assert "toolbar.classList.contains('bulk-toolbar-wave')" in script
    assert "toolbar.classList.contains('bulk-toolbar-pack')" in script
    # Scoped to the same ancestor the CSS counters are scoped to.
    assert "checkbox.closest('.table-wrap')" in script
    assert "tableWrap.querySelectorAll('tbody '" in script


def test_live_region_script_only_reacts_to_checkbox_changes(tmp_path, monkeypatch):
    """Scoped narrowly, per the follow-up's own instruction -- this must
    not become a general-purpose page script that reacts to anything
    else on the page."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    script = html[html.index("<script>"):html.index("</script>") + len("</script>")]
    assert "input[type=\"checkbox\"]" in script
    assert "form.submit" not in script
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script


# --- pages with no bulk toolbar get no script at all --------------------

def test_pages_without_a_bulk_toolbar_get_no_live_region_script(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/consignors").text
    assert "bulk-toolbar-count-live" not in html
    assert "<script>" not in html
