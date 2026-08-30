"""UX/design-system epic, item 22: site-wide accessibility audit and
remediation pass.

Not a single page -- a full-site pass against Section 14's WCAG 2.2 AA
criteria, verified with a real axe-core sweep across 27 real pages
(operator app + consignor portal, logged in and out) plus manual
keyboard/heading-order/zoom-reflow checks on the four most complex
pages (Pick Wave Detail, Inventory Sync, Competitive Pricing, Consignor
Detail), not guessed. The sweep found the "Phase 1 already added
semantic landmarks" claim was NOT actually true -- there was no <main>
anywhere in the app -- plus five other real, previously-unnoticed
defects: no <html lang>, ~72 form fields with a visually-adjacent but
not programmatically-associated <label>, a genuine contrast regression
in item 17's own .sync-stage-upcoming CSS (opacity: 0.6 halved an
otherwise-compliant color to ~3.3:1), a duplicate id="is_consignment"
across two forms on the same /inventory/add page, and a redundant
brand-mark alt text duplicating adjacent visible text. No skip-navigation
link existed at all before this item; it's entirely new. This file
spot-checks the mechanical, testable pieces of that remediation --
final verification that violations actually cleared was a live axe-core
run against a seeded database, not something a unit test can replay.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'accessibility_item22.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


# --- page shell: <main>, <html lang>, skip link -------------------------

def test_html_has_lang_attribute(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<html lang="en">' in html


def test_page_has_a_main_landmark_wrapping_content(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<main id="main-content" tabindex="-1">' in html
    assert "</main>" in html
    # <main> must open before real page content and close before the footer.
    assert html.index('<main id="main-content"') < html.index("<h1")
    assert html.index("</main>") < html.index('class="app-footer"')


def test_skip_link_is_present_and_targets_main_content(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert (
        '<a href="#main-content" class="skip-link">Skip to main content</a>'
    ) in html
    # It's the first thing in <body>, before <nav>.
    body = html[html.index("<body>"):]
    assert body.index("skip-link") < body.index("<nav>")


def test_portal_shell_also_has_skip_link_and_main_landmark(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/portal/login").text
    assert 'class="skip-link"' in html
    assert '<main id="main-content" tabindex="-1">' in html


def test_shipment_sync_banner_renders_inside_main_not_before_it(tmp_path, monkeypatch):
    """Regression guard: the banner originally sat between </nav> and
    <main>, outside every landmark (axe 'region' violation, 1 node per
    page) -- it now renders as the first thing inside <main>."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="stuck-1", source="manapool", status="shipped",
        )
        session.add(order)
        session.commit()
    html = TestClient(main.app).get("/inventory").text
    assert "failed to sync to Mana Pool" in html
    assert html.index('<main id="main-content"') < html.index("failed to sync to Mana Pool")


# --- nav-toggle checkbox: real gap, only found because its <label> is
# display:none at desktop width (for/id pairing alone isn't enough) -----

def test_nav_toggle_checkbox_has_an_aria_label(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert (
        'id="nav-toggle-checkbox" class="nav-toggle-checkbox" '
        'aria-label="Toggle navigation menu"'
    ) in html


def test_brand_mark_image_alt_is_decorative(tmp_path, monkeypatch):
    """Regression guard: alt="CardFoundry" duplicated the adjacent visible
    "CardFoundry" brand-name text (axe image-redundant-alt, every page)."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert 'src="/static/cardfoundry_favicon_pedestal.png" alt=""' in html


# --- duplicate id fix ----------------------------------------------------

def test_is_consignment_checkbox_id_is_not_duplicated_on_add_inventory_page(
    tmp_path, monkeypatch,
):
    """Regression guard: /inventory/add renders two different "new batch"
    forms, both of which used to hardcode id="is_consignment" -- a real
    duplicate-id-active violation. The id served no purpose (label
    association there uses implicit wrapping, not for/id) so it's
    removed rather than uniquified."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory/add").text
    assert html.count('id="is_consignment"') == 0
    assert html.count('name="is_consignment"') >= 2


# --- label association: spot-check a few of the ~57 wrapped instances --

def test_new_consignor_form_labels_are_wrapped_not_bare(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/consignors/new").text
    assert '<label>Name<br>\n        <input type="text" name="name" required>' in html
    # The old, unassociated shape must be gone.
    assert "<label>Name</label><br>" not in html


def test_select_fields_without_visible_label_text_get_aria_label(tmp_path, monkeypatch):
    """Selects that sit next to a radio button's label (not their own)
    have no visible text of their own to wrap -- aria-label is the
    correct technique there, not the implicit-wrap pattern used
    elsewhere in this same pass."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory/add").text
    assert 'aria-label="Target batch"' in html


# --- bulk-selection checkboxes: per-row aria-label ----------------------

def test_bulk_select_order_checkbox_has_a_per_row_aria_label(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(SalesOrder(
            external_order_id="ORD-9001", source="manapool",
            status="ready_to_pick",
        ))
        session.commit()
    html = TestClient(main.app).get("/orders").text
    assert 'aria-label="Select order ORD-9001"' in html


# --- sort state exposed to assistive tech, not just visually -----------

def test_sortable_column_headers_expose_aria_sort(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Forest", set_code="SET", collector_number="1",
            scryfall_id="sf-1", condition="LP", condition_id="LP", finish="normal",
            finish_id="NF", language_id="EN", status="available",
        ))
        session.commit()
    html = TestClient(main.app).get("/inventory?sort=name&direction=asc&show_all=true").text
    assert '<th class="sort-active" aria-sort="ascending">' in html
    # A non-active sortable column must not claim a sort state.
    assert '<th aria-sort=' not in html.replace(
        '<th class="sort-active" aria-sort="ascending">', '', 1,
    )


# --- contrast regression fix (item 17's own CSS) ------------------------

def test_sync_stage_upcoming_no_longer_uses_opacity(tmp_path, monkeypatch):
    """Regression guard: opacity: 0.6 halved this pill's contrast against
    --cf-surface (~6.9:1 down to ~3.3:1, failing WCAG AA for its small
    text). Dashed border conveys "not yet reached" without touching
    text contrast."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory-sync").text
    assert ".sync-stage-upcoming {" in html
    stage_css = html[html.index(".sync-stage-upcoming {"):]
    stage_css = stage_css[:stage_css.index("}")]
    declarations = stage_css[stage_css.index("*/") + 2:]
    assert "opacity" not in declarations
    assert "border-style: dashed;" in declarations


# --- prerequisite: /inventory/add's consignor select is wrapped, not
# aria-label'd (it DOES have its own visible label text) ----------------

def test_consignor_select_uses_wrapped_label_not_aria_label(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        session.add(Consignor(name="Jane Doe", is_active=True))
        session.commit()
    html = TestClient(main.app).get("/inventory/add").text
    assert '<label>Consignor (required if consignment)<br>' in html
    assert "</select></label><br>" in html
