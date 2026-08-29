"""UX/design-system epic, item 4: eliminate page-level horizontal
overflow, site-wide.

Deferred until the shared .data-table/.bulk-toolbar component (item 8,
Phase 2 part 2) landed so the six in-scope tables wouldn't get built
twice. Strategy per table, decided from real content measured live on
production (a 57-char double-faced card name, 36-char order/job UUIDs,
a 33-char pricing action, etc.) rather than estimated from column
counts alone -- two of the six (Pick Waves, Consignors) looked short
enough to skip on a column-count guess but measured real overflow at
320px in a live Chromium render.

All six tables use the same strategy: a contained (not page-level)
horizontal-scroll region (.data-table-scroll wrapping .data-table).
A card/list transform was considered and rejected for all six -- they
are dense operational/history lists, and choosing what to hide/reflow
at narrow widths is a workflow-design decision that belongs to each
page's own later redesign phase, not this "stop the scroll" item.

These tests check structure/markup (always safe to run, no new
dependency), not actual rendered overflow -- that was verified directly
with a real headless-Chromium render (Playwright) during development:
0 of 36 checks (6 tables x 6 widths: 320/390/600/1024/1440/1920px)
showed page-level horizontal overflow, confirmed against real
production-scale content. See the CHANGELOG for the full measured
before/after numbers.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, Consignor, PickWave


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'phase2_overflow.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_data_table_scroll_css_is_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".data-table-scroll {"):]
    rule = rule[:rule.index("}") + 1]
    assert "overflow-x: auto;" in rule
    assert "max-width: 100%;" in rule


def test_data_table_scroll_child_table_has_min_width(tmp_path, monkeypatch):
    """min-width:100% keeps a short table (e.g. Pick Waves) from
    collapsing narrower than its container -- purely cosmetic, doesn't
    affect the overflow behavior itself."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".data-table-scroll .data-table {" in html


def test_checkbox_touch_target_media_query_scoped_to_compact_and_tablet(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert "@media (max-width: 1023px)" in html
    idx = html.index('.data-table input[type="checkbox"]')
    rule = html[idx:html.index("}", idx) + 1]
    assert "width: 24px;" in rule
    assert "height: 24px;" in rule


# --- all six tables wrapped in .data-table-scroll -------------------------

def test_inventory_search_table_wrapped_in_scroll_region(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table data-table-cards density-compact">')
    assert scroll_idx < table_idx


def test_orders_table_wrapped_in_scroll_region(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table density-compact">')
    assert scroll_idx < table_idx


def test_pick_waves_migrated_to_data_table_and_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(PickWave(label="Wave 2026-08-13 02:26 PM", status="active"))
        session.commit()
    html = TestClient(main.app).get("/pick-waves").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table density-comfortable">')
    assert scroll_idx < table_idx


def test_pricing_migrated_to_data_table_and_wrapped(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pricing").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table density-comfortable">')
    assert scroll_idx < table_idx


def test_inventory_sync_migrated_to_data_table_and_wrapped(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory-sync").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table density-comfortable">')
    assert scroll_idx < table_idx


def test_consignors_migrated_to_data_table_and_wrapped(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="CameronRochelle", payout_method="CashApp", is_active=True))
        session.commit()
    html = TestClient(main.app).get("/consignors").text
    body = html[html.index("<body>"):]
    scroll_idx = body.index('<div class="data-table-scroll">')
    table_idx = body.index('<table class="data-table density-comfortable">')
    assert scroll_idx < table_idx


# --- empty states also use the shared class, on all migrated tables ------

def test_pick_waves_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pick-waves").text
    assert 'class="data-table-empty"' in html
    assert "No pick waves yet." in html


def test_pricing_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pricing").text
    assert 'class="data-table-empty"' in html
    assert "No pricing jobs yet." in html


def test_inventory_sync_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory-sync").text
    assert 'class="data-table-empty"' in html
    assert "No inventory-sync previews yet." in html


def test_consignors_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/consignors").text
    assert 'class="data-table-empty"' in html
    assert "No consignors yet." in html


# --- no functional regression: filters/columns/data still present --------

def test_inventory_search_sort_links_and_filters_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert 'name="q"' in html
    assert 'name="batch"' in html
    assert 'name="status"' in html
    assert "sort=name" in html


def test_orders_status_tabs_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    assert 'class="status-tabs no-print"' in html


def test_pick_waves_columns_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pick-waves").text
    for col in ("Wave", "Orders", "Status", "Created"):
        assert f"<th>{col}</th>" in html


def test_pricing_columns_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pricing").text
    for col in ("ID", "Action", "Status", "Mana Pool Job ID", "Created"):
        assert f"<th>{col}</th>" in html


def test_consignors_columns_and_links_unchanged(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/consignors").text
    for col in ("Name", "Payout Method", "Status"):
        assert f"<th>{col}</th>" in html
    assert 'href="/consignors/new"' in html
    assert 'href="/consignors/owed"' in html
