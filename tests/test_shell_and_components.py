"""Phase 1 continued of the UX/design-system epic: responsive application
shell + navigation, and the core component library (buttons, forms, focus
states, page header, breadcrumbs).

This slice consumes the v1.76.0 token set exclusively (no ad hoc color/
spacing/typography values), replaces the flat nav row with a three-tier
grouped + active-state + mobile-disclosure shell, adds a reusable
page-header component wired into three representative pages, and fixes
the two contrast/legibility issues the token work flagged but didn't fix
(button:hover, form-control font size). No individual workflow page's
content/layout is redesigned here beyond picking up the new shell.
"""

import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'shell_and_components.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


NAV_LINKS = [
    ("/inventory", "Inventory Search"),
    ("/orders", "Orders"),
    ("/pick-waves", "Pick Waves"),
    ("/pricing", "Price Updates"),
    ("/inventory-sync", "Inventory Sync"),
    ("/consignors", "Consignors"),
    ("/admin", "Admin"),
]


def _nav_link_classes(html: str) -> dict:
    """Map href -> class attribute string for every rendered nav-link."""
    out = {}
    for m in re.finditer(r'<a href="([^"]+)" class="(nav-link[^"]*)">', html):
        out[m.group(1)] = m.group(2)
    return out


# --- nav: all links present, grouped, no JS -----------------------------

def test_nav_still_reaches_every_existing_destination(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory")
    for href, label in NAV_LINKS:
        assert f'href="{href}"' in response.text
        assert label in response.text


def test_nav_is_grouped_into_three_tiers_with_dividers(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert 'class="nav-group nav-group-daily"' in html
    assert 'class="nav-group nav-group-ops"' in html
    assert 'class="nav-group nav-group-admin"' in html
    assert html.count('class="nav-divider"') == 2


def test_nav_groups_contain_the_right_links(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body = html[html.index("<body>"):]

    def group_html(marker):
        start = body.index(f'class="nav-group {marker}"')
        return body[start:body.index("</div>", start)]

    daily = group_html("nav-group-daily")
    ops = group_html("nav-group-ops")
    admin = group_html("nav-group-admin")
    for label in ("Inventory Search", "Orders", "Pick Waves"):
        assert label in daily
    for label in ("Price Updates", "Inventory Sync", "Consignors"):
        assert label in ops
    assert "Admin" in admin


def test_nav_uses_checkbox_label_disclosure_no_javascript(tmp_path, monkeypatch):
    """v1.77.3: <details> is out entirely. It renders its non-summary
    content through an internal user-agent shadow tree (a <slot>, visible
    in DevTools) whose slot-assignment layer doesn't reliably honor
    light-DOM display/content-visibility overrides -- confirmed live via
    a DevTools computed-styles inspection after two prior CSS-only fixes
    (v1.77.1, v1.77.2) both failed to make the content actually paint.
    Replaced with the classic checkbox+label CSS toggle: plain elements,
    no shadow DOM, nothing left to fight."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body = html[html.index("<body>"):]
    assert "<details" not in body
    assert "<summary" not in body
    assert (
        '<input type="checkbox" id="nav-toggle-checkbox" '
        'class="nav-toggle-checkbox" aria-label="Toggle navigation menu">'
    ) in html
    assert 'for="nav-toggle-checkbox"' in html
    assert "<script" not in html.lower()
    nav = html[html.index("<nav>"):html.index("</nav>")]
    assert "onclick" not in nav.lower()
    assert "onsubmit" not in nav.lower()


def test_nav_links_are_direct_siblings_of_the_toggle_checkbox(tmp_path, monkeypatch):
    """The checkbox+label pattern depends on .nav-toggle-checkbox and
    .nav-links being siblings, checkbox first in source order, so the
    `~` general-sibling selector in the mobile media query can reach it."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body = html[html.index("<body>"):]
    checkbox_idx = body.index('id="nav-toggle-checkbox"')
    nav_links_idx = body.index('class="nav-links"')
    assert checkbox_idx < nav_links_idx


def test_mobile_checked_selector_targets_nav_links(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".nav-toggle-checkbox:checked ~ .nav-links" in html


def test_nav_toggle_checkbox_is_visually_hidden_but_not_display_none(tmp_path, monkeypatch):
    """display:none would remove it from the tab order too -- it must
    stay keyboard-focusable/operable, just not visible on-screen."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".nav-toggle-checkbox {"):]
    rule = rule[:rule.index("}") + 1]
    assert "display: none" not in rule
    assert "clip: rect(0, 0, 0, 0);" in rule


def test_nav_toggle_checkbox_focus_ring_visible_on_its_label(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert ".nav-toggle-checkbox:focus-visible + .nav-toggle-summary {" in html


# --- nav: active-section state -------------------------------------------

def test_active_state_on_inventory_search(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    classes = _nav_link_classes(html)
    assert classes["/inventory"] == "nav-link active"
    assert classes["/orders"] == "nav-link"
    assert classes["/inventory-sync"] == "nav-link"


def test_active_state_on_orders(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    classes = _nav_link_classes(html)
    assert classes["/orders"] == "nav-link active"
    assert classes["/inventory"] == "nav-link"


def test_active_state_on_pick_waves(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pick-waves").text
    classes = _nav_link_classes(html)
    assert classes["/pick-waves"] == "nav-link active"


def test_active_state_on_inventory_sync_is_not_confused_with_inventory(tmp_path, monkeypatch):
    """/inventory-sync must resolve to its own section, not fall through
    to the shorter /inventory prefix."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory-sync").text
    classes = _nav_link_classes(html)
    assert classes["/inventory-sync"] == "nav-link active"
    assert classes["/inventory"] == "nav-link"


def test_active_state_on_admin(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/admin").text
    classes = _nav_link_classes(html)
    assert classes["/admin"] == "nav-link active"


def test_active_state_on_consignors(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/consignors").text
    classes = _nav_link_classes(html)
    assert classes["/consignors"] == "nav-link active"


def test_no_active_state_on_pages_outside_the_nav_prefixes(tmp_path, monkeypatch):
    """A page whose path doesn't match any known section (e.g. the
    consignor portal login) renders the nav with nothing highlighted,
    rather than crashing or guessing."""
    setup_db(tmp_path, monkeypatch)
    # exercise directly: an unrecognized path resolves to "" (no match)
    token = main._current_request_path.set("/some/unmapped/path")
    try:
        assert main._active_nav_section() == ""
    finally:
        main._current_request_path.reset(token)


# --- mobile breakpoint CSS -------------------------------------------------

def test_mobile_breakpoint_media_query_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert "@media (max-width: 599px)" in html
    assert ".nav-toggle-checkbox:checked ~ .nav-links" in html


# --- focus states -----------------------------------------------------------

def test_focus_visible_rule_covers_every_interactive_element(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    focus_block = html.split(":focus-visible,")[0].split("a:focus-visible")[-1]
    # the full selector list precedes the shared outline declaration
    idx = html.index("a:focus-visible")
    selector_list = html[idx:html.index("{", idx)]
    for selector in ("a:focus-visible", "button:focus-visible", "input:focus-visible",
                      "textarea:focus-visible", "select:focus-visible", "summary:focus-visible"):
        assert selector in selector_list
    assert "outline: var(--cf-focus-ring-width) solid var(--cf-focus-ring);" in html
    assert "outline-offset: var(--cf-focus-ring-offset);" in html


# --- the two token-flagged fixes -------------------------------------------

def test_button_hover_no_longer_uses_the_failing_accent_bright_pairing(tmp_path, monkeypatch):
    """v1.76.0 flagged button:hover at 2.34:1 (fails 3:1) because it paired
    --cf-accent-bright with white button text. Fixed to --cf-accent-hover
    (6.26:1)."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    hover_rule = html[html.index("button:hover"):]
    hover_rule = hover_rule[:hover_rule.index("}")]
    assert "var(--cf-accent-hover)" in hover_rule
    assert "var(--cf-accent-bright)" not in hover_rule


def test_card_view_link_hover_gets_the_same_contrast_fix(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".card-view-link:hover"):]
    rule = rule[:rule.index("}")]
    assert "var(--cf-accent-hover)" in rule


def test_form_control_font_size_no_longer_falls_back_to_browser_default(tmp_path, monkeypatch):
    """v1.76.0 flagged input/textarea/select at ~13.3px (browser default,
    no explicit font-size). Fixed to --cf-text-body (1rem)."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index("input,\n"):]
    rule = rule[:rule.index("}") + 1]
    assert "font-size: var(--cf-text-body);" in rule
    assert "border: 1px solid var(--cf-border-strong);" in rule


# --- button variants ----------------------------------------------------

def test_button_variant_classes_are_all_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    for cls in (".btn-primary", ".btn-secondary", ".btn-tertiary",
                ".btn-destructive", ".btn-icon", ".btn-loading"):
        assert cls in html


def test_only_one_primary_styled_button_survives_as_the_bare_selector(tmp_path, monkeypatch):
    """Bare <button> stays the single 'primary' look app-wide (unchanged
    default) rather than every variant independently claiming it -- this
    is what keeps orange from meaning six different things at once."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert "button,\n                .btn-primary {" in html


def test_destructive_button_uses_the_verified_danger_solid_pairing(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".btn-destructive {"):]
    rule = rule[:rule.index("}") + 1]
    assert "var(--cf-danger-solid)" in rule
    assert "var(--cf-danger-solid-text)" in rule


def test_disabled_and_loading_states_use_the_shared_opacity_tokens(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert "opacity: var(--cf-disabled-opacity);" in html
    loading_rule = html[html.index(".btn-loading {"):]
    loading_rule = loading_rule[:loading_rule.index("}") + 1]
    assert "opacity: var(--cf-loading-opacity);" in loading_rule
    assert "pointer-events: none;" in loading_rule


def test_link_muted_variant_is_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert "a.link-muted {" in html


# --- page header component, wired into 3 representative pages -----------

def test_inventory_search_uses_page_header_with_primary_and_secondary_actions(tmp_path, monkeypatch):
    """UX epic item 9: "Add Inventory" is the page's unambiguous primary
    action, promoted from btn-secondary to btn-primary."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<header class="page-header">' in html
    assert '<h1 class="page-header-title">' in html and "Inventory Search" in html
    assert 'href="/inventory/add" class="btn-primary"' in html
    assert "Show All Inventory" in html
    assert 'name="show_all"' in html


def test_orders_uses_page_header_with_meta_and_primary_action(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/orders").text
    assert '<header class="page-header">' in html
    assert '<h1 class="page-header-title">' in html and "Orders" in html
    assert 'class="page-header-meta"' in html
    assert "Sync Mana Pool Orders" in html
    # sync action now lives in the header, not duplicated in the body
    assert html.count("Sync Mana Pool Orders") == 1


def test_pick_waves_uses_page_header_with_description(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/pick-waves").text
    assert '<header class="page-header">' in html
    assert '<h1 class="page-header-title">' in html and "Pick Waves" in html
    assert 'class="page-header-description"' in html
    assert "Pick waves combine fully allocated orders" in html


def test_page_header_breadcrumbs_present_on_all_three_proof_pages(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for path in ("/inventory", "/orders", "/pick-waves"):
        html = client.get(path).text
        assert 'class="breadcrumbs"' in html
        assert 'aria-label="Breadcrumb"' in html


def test_page_header_helper_escapes_plain_text_fields():
    html = main._page_header(
        "<b>Title</b>",
        description="<i>desc</i>",
    )
    assert "<b>Title</b>" not in html
    assert "&lt;b&gt;Title&lt;/b&gt;" in html
    assert "&lt;i&gt;desc&lt;/i&gt;" in html


def test_page_header_omits_empty_slots():
    html = main._page_header("Just a Title")
    assert "page-header-description" not in html
    assert "page-header-actions" not in html
    assert "page-header-meta" not in html


def test_page_header_renders_both_action_slots_together():
    html = main._page_header(
        "Widgets",
        primary_action='<button class="btn-primary">Do It</button>',
        secondary_actions='<button class="btn-secondary">Also This</button>',
    )
    assert '<div class="page-header-actions">' in html
    assert "Do It" in html
    assert "Also This" in html


# --- breadcrumbs component ------------------------------------------------

def test_breadcrumbs_renders_links_and_current_crumb():
    html = main._breadcrumbs([("CardFoundry", "/inventory"), ("Inventory Search", None)])
    assert '<a href="/inventory">CardFoundry</a>' in html
    assert '<span class="breadcrumb-current">Inventory Search</span>' in html
    assert 'class="breadcrumb-sep"' in html
    assert html.count('class="breadcrumb-sep"') == 1


def test_breadcrumbs_escapes_labels_and_hrefs():
    html = main._breadcrumbs([("<script>", "/x?a=<b>"), ("Current", None)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- form field / field group component ----------------------------------

def test_form_field_renders_persistent_label_not_placeholder_only():
    html = main._form_field(
        "Card name", '<input type="text" name="q">', field_id="q",
    )
    assert '<label class="form-field-label" for="q">Card name</label>' in html
    assert '<input type="text" name="q">' in html


def test_form_field_required_marker():
    html = main._form_field("Email", "<input>", required=True)
    assert 'class="form-field-required"' in html


def test_form_field_error_state_adds_class_and_message():
    html = main._form_field("Email", "<input>", error="Must be a valid address.")
    assert "form-field-has-error" in html
    assert '<p class="form-field-error">Must be a valid address.</p>' in html


def test_form_field_help_text():
    html = main._form_field("Email", "<input>", help_text="We'll never share this.")
    assert '<p class="form-field-help">We&#x27;ll never share this.</p>' in html


def test_form_field_and_error_css_classes_are_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    for cls in (".form-field {", ".form-field-label {", ".form-field-help {",
                ".form-field-error {", ".form-field-has-error input"):
        assert cls in html


# --- container/spacing consistency + footer -------------------------------

def test_body_uses_container_and_spacing_tokens_not_hardcoded_values(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    body_rule = html[html.index("body {"):]
    body_rule = body_rule[:body_rule.index("}") + 1]
    assert "max-width: var(--cf-container-max);" in body_rule
    assert "margin: var(--cf-space-6) auto;" in body_rule
    assert "padding: 0 var(--cf-space-5);" in body_rule
    assert "1200px" not in body_rule
    assert "40px" not in body_rule


def test_footer_uses_consistent_app_footer_class(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    assert '<footer class="app-footer">' in html
    assert f"CardFoundry v{main.APP_VERSION}" in html


def test_footer_css_is_token_driven(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/inventory").text
    rule = html[html.index(".app-footer {"):]
    rule = rule[:rule.index("}") + 1]
    assert "var(--cf-space-7)" in rule
    assert "var(--cf-border)" in rule
    assert "var(--cf-text-muted)" in rule


# --- portal shell is untouched ---------------------------------------------

def test_consignor_portal_shell_still_has_no_operator_nav(tmp_path, monkeypatch):
    """The portal's own minimal shell (_portal_page_start) is deliberately
    separate from the operator nav and out of scope for this slice."""
    setup_db(tmp_path, monkeypatch)
    html = TestClient(main.app).get("/portal").text
    body = html[html.index("<body>"):]
    assert 'class="nav-group nav-group-daily"' not in body
    assert "Inventory Search" not in body
