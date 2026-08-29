"""UX/design-system epic, item 9: Inventory Search responsive redesign.

Covers what isn't already exercised by the pre-existing pagination/
card-view-link/decklist-route/shell-component test files: tabs on the
search page itself, sort-active header highlighting, right-aligned
tabular-nums price cells, the dominant card-name treatment, the
row-actions disclosure menu, data-label attributes (needed by the
narrow-width card transform), the .data-table-cards responsive CSS,
the page-size selector, and the empty-state colspan.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory_search_item9.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, name="Forest", scryfall_id="sf-1", batch_code="A1", **kwargs):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code, is_archived=False)
        session.add(batch)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="SET", collector_number="1",
        scryfall_id=scryfall_id, condition="LP", condition_id="LP", finish="normal",
        finish_id="NF", language_id="EN", status="available", **kwargs,
    )
    session.add(card)
    session.commit()
    return card


# --- tabs -------------------------------------------------------------

def test_search_page_shows_real_tabs_not_select_and_switch_button(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<nav class="tabs" aria-label="Search mode">' in html
    assert '<select name="mode">' not in html
    assert ">Switch<" not in html


# --- sort-active header highlighting -----------------------------------

def test_sort_active_class_lands_on_the_active_column_only(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Alpha")
    html = TestClient(main.app).get("/inventory?sort=current_price&show_all=true").text
    assert '<th class="num sort-active">' in html
    header = html[html.index("<thead>"):html.index("</thead>")]
    assert header.count('sort-active') == 1


def test_sort_active_defaults_to_name_column(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Alpha")
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<th class="sort-active">' in html
    header = html[html.index("<thead>"):html.index("</thead>")]
    assert header.count('sort-active') == 1


# --- price alignment / formatting --------------------------------------

def test_price_cells_are_right_aligned_and_tabular(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(
            session, name="Alpha", bought_in_price=1.5, current_price=12.0, sold_price=20.25,
        )
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<td class="num cf-tabular-nums" data-label="Current Price">' in html
    assert '<td class="num cf-tabular-nums" data-label="Bought-In">' in html
    assert '<td class="num cf-tabular-nums" data-label="Sold Price">' in html
    assert "$1.50" in html
    assert "$20.25" in html


# --- card name as the dominant value -----------------------------------

def test_card_name_cell_carries_its_own_dominant_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Alpha")
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<td class="card-name" data-label="Card">Alpha' in html


# --- row-actions menu ----------------------------------------------------

def test_row_actions_menu_present_when_reference_links_exist(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Alpha", scryfall_id="sf-alpha")
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<details class="row-actions">' in html
    assert '<summary aria-label="More actions">' in html
    assert '<div class="row-actions-menu">' in html
    # Edit stays a direct, always-visible link, not tucked in the menu.
    assert 'href="/inventory/1/edit">Edit</a>' in html.split('<details class="row-actions">')[0][-60:]


def test_row_actions_menu_absent_when_no_reference_links(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Mystery", scryfall_id=None)
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<details class="row-actions">' not in html


# --- data-label attributes (drive the narrow-width card transform) -----

def test_every_data_cell_has_a_data_label_for_the_card_transform(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        add_card(session, name="Alpha")
    html = TestClient(main.app).get("/inventory?show_all=true").text
    for label in (
        "Card", "Set", "Collector #", "Finish", "Condition", "Batch",
        "Status", "Exception", "Current Price", "Bought-In", "Sold Price", "Actions",
    ):
        assert f'data-label="{label}"' in html


def test_table_opts_into_the_data_table_cards_transform(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<table class="data-table data-table-cards density-compact">' in html
    assert "@media (max-width: 1023px)" in html
    assert ".data-table-cards thead" in html
    assert ".data-table-cards td[data-label]::before" in html


# --- empty state ---------------------------------------------------------

def test_empty_state_colspan_matches_thirteen_columns(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory?show_all=true").text
    assert '<td colspan="13" class="data-table-empty">' in html


# --- page-size selector ---------------------------------------------------

def test_page_size_selector_defaults_to_twenty_five_and_lists_all_options(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<select id="inv-page-size" name="page_size">' in html
    assert '<option value="25" selected>25</option>' in html
    assert '<option value="50" >50</option>' in html
    assert '<option value="100" >100</option>' in html


def test_page_size_selector_reflects_explicit_choice(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory?page_size=50").text
    assert '<option value="25" >25</option>' in html
    assert '<option value="50" selected>50</option>' in html


def test_invalid_page_size_falls_back_to_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory?page_size=9999").text
    assert '<option value="25" selected>25</option>' in html


# --- filter labels are persistent, not placeholder-only -----------------

def test_every_filter_has_a_persistent_visible_label(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<label class="form-field-label" for="inv-q">Card name</label>' in html
    assert '<label class="form-field-label" for="inv-batch">Batch</label>' in html
    assert '<label class="form-field-label" for="inv-status">Status</label>' in html
    assert '<label class="form-field-label" for="inv-exception">Exception state</label>' in html
    assert '<label class="form-field-label" for="inv-page-size">Rows per page</label>' in html
