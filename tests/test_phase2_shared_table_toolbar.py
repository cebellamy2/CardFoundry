"""Phase 2, part 2 of the UX/design-system epic: the shared table
component and the shared bulk-action toolbar.

Re-skin, not a rebuild: the existing no-JS checkbox+form mechanism on
Inventory Search and Orders is unchanged (same routes, same endpoints,
same interaction model). This wires both pages onto shared markup/CSS
(.data-table, .table-wrap, .bulk-toolbar) and merges what were two
near-identical bulk-action result pages into one shared component.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'phase2_table.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


# --- shared table CSS ----------------------------------------------------

def test_data_table_css_classes_are_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".data-table {" in html
    assert ".data-table.density-compact th," in html
    assert ".data-table.density-comfortable th," in html
    assert "var(--cf-table-cell-padding-compact)" in html
    assert "var(--cf-table-cell-padding-comfortable)" in html


def test_data_table_empty_state_class_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".data-table-empty {" in html


def test_bare_table_th_td_rules_are_untouched_for_other_pages(tmp_path, monkeypatch):
    """Every table that hasn't been migrated to .data-table this slice
    must keep looking exactly as it did before -- confirmed by the
    original bare table/th/td rule still being present and unchanged."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    idx = html.index("table {")
    rule = html[idx:html.index("}", idx) + 1]
    assert "border-collapse: collapse;" in rule
    assert "margin-top: 20px;" in rule


# --- bulk toolbar CSS (:has() + counters, no JS) --------------------------

def test_bulk_toolbar_hidden_by_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".bulk-toolbar {"):]
    rule = rule[:rule.index("}") + 1]
    assert "display: none;" in rule


def test_bulk_toolbar_any_visibility_rule_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '.table-wrap:has(tbody input[type="checkbox"]:checked) .bulk-toolbar.bulk-toolbar-any' in html


def test_bulk_toolbar_wave_and_pack_are_scoped_to_distinct_checkbox_names(tmp_path, monkeypatch):
    """Orders has two mutually-exclusive checkbox groups per row -- each
    toolbar's visibility must key off its own group's name attribute,
    not any checkbox in the table, or checking a wave-eligible row would
    also surface the unrelated pack toolbar."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    assert '.table-wrap:has(tbody input[name="order_ids"]:checked) .bulk-toolbar.bulk-toolbar-wave' in html
    assert '.table-wrap:has(tbody input[name="pack_order_ids"]:checked) .bulk-toolbar.bulk-toolbar-pack' in html


def test_selected_count_uses_three_independent_counters(tmp_path, monkeypatch):
    """A shared counter would show a combined, misleading number in both
    Orders toolbars if a user ever checked one row of each kind at once."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    assert "counter-reset: cf-any-count cf-wave-count cf-pack-count;" in html
    assert 'counter-increment: cf-wave-count;' in html
    assert 'counter-increment: cf-pack-count;' in html
    assert ".bulk-toolbar-wave .bulk-toolbar-count::before" in html
    assert ".bulk-toolbar-pack .bulk-toolbar-count::before" in html


def test_no_javascript_added_anywhere_in_the_toolbar_mechanism(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    for path in ("/inventory", "/orders"):
        html = TestClient(main.app).get(path).text
        assert "<script" not in html.lower()


# --- integration: pages wired onto the shared table -----------------------

def test_inventory_search_table_and_toolbar_share_one_table_wrap(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body = html[html.index("<body>"):]
    wrap_start = body.index('<div class="table-wrap">')
    wrap_end = body.index("</table>", wrap_start)
    wrap = body[wrap_start:wrap_end]
    assert 'class="bulk-toolbar bulk-toolbar-any no-print"' in wrap
    assert 'class="data-table density-compact"' in wrap


def test_orders_table_and_both_toolbars_share_one_table_wrap(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    body = html[html.index("<body>"):]
    wrap_start = body.index('<div class="table-wrap">')
    wrap_end = body.index("</table>", wrap_start)
    wrap = body[wrap_start:wrap_end]
    assert 'class="bulk-toolbar bulk-toolbar-wave no-print"' in wrap
    assert 'class="bulk-toolbar bulk-toolbar-pack no-print"' in wrap
    assert 'class="data-table density-compact"' in wrap


def test_batches_page_bulk_toolbar_still_wrapped_correctly(tmp_path, monkeypatch):
    """_bulk_card_action_form is shared with /batches/{id} -- changing its
    CSS class to the new toolbar mechanism means that page's table also
    needs a .table-wrap or its toolbar would be permanently hidden
    (display:none with no matching :has() ancestor context)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.commit()
        batch_id = batch.id
    html = TestClient(main.app).get(f"/batches/{batch_id}").text
    body = html[html.index("<body>"):]
    wrap_start = body.index('<div class="table-wrap">')
    wrap_end = body.index("</table>", wrap_start)
    wrap = body[wrap_start:wrap_end]
    assert 'class="bulk-toolbar bulk-toolbar-any no-print"' in wrap


def test_inventory_search_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory?q=NoSuchCardAtAll").text
    assert 'class="data-table-empty"' in html
    assert "No cards found." in html


def test_orders_empty_state_uses_shared_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders?status=cancelled").text
    assert 'class="data-table-empty"' in html


# --- merged bulk-action result page ---------------------------------------

def test_bulk_outcome_badge_success_role():
    html = main._bulk_outcome_badge("moved")
    assert 'class="badge badge-success"' in html
    assert "Moved</span>" in html


def test_bulk_outcome_badge_failure_roles():
    for outcome in ("skipped", "blocked"):
        html = main._bulk_outcome_badge(outcome)
        assert 'class="badge badge-danger"' in html


def test_bulk_action_result_page_renders_link_when_present():
    html = main._bulk_action_result_page(
        "Test Results",
        [{"outcome": "packed", "name": "ord-1", "link": "/orders/1", "reason": ""}],
        "/orders",
    )
    assert '<a href="/orders/1">ord-1</a>' in html


def test_bulk_action_result_page_renders_plain_text_when_no_link():
    html = main._bulk_action_result_page(
        "Test Results",
        [{"outcome": "moved", "name": "Bolt", "link": None, "reason": ""}],
        "/inventory",
    )
    table = html[html.index("<table"):html.index("</table>")]
    assert "<a href=" not in table
    assert "Bolt" in table


def test_bulk_action_result_page_uses_outcome_banner():
    html = main._bulk_action_result_page(
        "Test Results",
        [{"outcome": "packed", "name": "x", "link": None, "reason": ""}],
        "/orders",
    )
    assert 'class="outcome-banner outcome-banner-success"' in html


def test_bulk_action_result_page_banner_kind_all_failed():
    html = main._bulk_action_result_page(
        "Test Results",
        [{"outcome": "skipped", "name": "x", "link": None, "reason": "nope"}],
        "/orders",
    )
    assert 'class="outcome-banner outcome-banner-danger"' in html


def test_bulk_action_result_page_banner_kind_partial():
    html = main._bulk_action_result_page(
        "Test Results",
        [
            {"outcome": "packed", "name": "a", "link": None, "reason": ""},
            {"outcome": "skipped", "name": "b", "link": None, "reason": "nope"},
        ],
        "/orders",
    )
    assert 'class="outcome-banner outcome-banner-warning"' in html


def test_bulk_action_result_page_uses_data_table():
    html = main._bulk_action_result_page(
        "Test Results", [], "/orders",
    )
    assert 'class="data-table density-compact"' in html


def test_bulk_action_result_page_custom_item_column():
    html = main._bulk_action_result_page(
        "Test Results", [], "/orders", item_column="Order",
    )
    assert "<th>Order</th>" in html


def test_bulk_action_result_page_default_back_label():
    html = main._bulk_action_result_page("Test Results", [], "/orders")
    assert ">Back<" in html


def test_bulk_action_result_page_custom_back_label():
    html = main._bulk_action_result_page(
        "Test Results", [], "/pick-waves/1", back_label="Back to Pick Wave",
    )
    assert ">Back to Pick Wave<" in html
