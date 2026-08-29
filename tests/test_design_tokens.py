"""Phase 1 of the UX/design-system epic: the design token set.

Foundation-only phase -- these tests confirm the tokens exist and are
declared with the values verified against real WCAG contrast ratios, not
that any page has been visually redesigned around them yet (most tokens
are declared but not wired into any rule this phase, by design).
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'design_tokens.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


# -- WCAG contrast, re-derived here (not imported) so a future edit to
# these token values gets caught by the same math used to choose them,
# without this test file depending on any app-internal helper. --------

def _linearize(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = _linearize(r), _linearize(g), _linearize(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_hex, bg_hex):
    l1, l2 = _luminance(fg_hex), _luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def get_root_css(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    return response.text


# -- neutrals: existing names kept stable, new tiers added --------------

def test_existing_named_tokens_are_preserved(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-bg: #0b0b0b" in css
    assert "--cf-surface: #161412" in css
    assert "--cf-accent: #c44a07" in css
    assert "--cf-accent-bright: #ff8b26" in css
    assert "--cf-border: #3a352d" in css


def test_new_surface_elevation_tiers_are_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-surface-elevated: #211e1a" in css
    assert "--cf-surface-elevated-hover: #2b2722" in css


def test_border_strong_was_adjusted_to_actually_clear_1_4_11(tmp_path, monkeypatch):
    """The ~#514a3f starting point only measured ~2:1 against surface/bg
    -- adjusted up to a value that genuinely clears the 3:1 non-text
    contrast a locatable UI-component boundary needs."""
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-border-strong: #746a5a" in css
    assert contrast_ratio("#746a5a", "#161412") >= 3.0
    assert contrast_ratio("#746a5a", "#0b0b0b") >= 3.0


def test_text_tiers_all_clear_aa_on_every_surface(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-text: #e7e2d9" in css
    assert "--cf-text-secondary: #c4beb3" in css
    assert "--cf-text-muted: #a59e92" in css
    surfaces = ["#0b0b0b", "#161412", "#211e1a", "#2b2722"]
    for text_hex in ("#e7e2d9", "#c4beb3", "#a59e92"):
        for surface_hex in surfaces:
            assert contrast_ratio(text_hex, surface_hex) >= 4.5, (
                f"{text_hex} on {surface_hex} fails AA"
            )


# -- brand orange: surface vs text role split, per deb5db0's precedent --

def test_accent_surface_role_passes_with_white_text(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-accent-hover: #a73f06" in css
    assert "--cf-accent-active: #933805" in css
    for accent_hex in ("#c44a07", "#a73f06", "#933805"):
        assert contrast_ratio("#ffffff", accent_hex) >= 4.5


def test_accent_bright_text_role_passes_on_bg_and_surface(tmp_path, monkeypatch):
    for accent_bright_hex in ("#ff8b26", "#ffa04d"):
        assert contrast_ratio(accent_bright_hex, "#0b0b0b") >= 4.5
        assert contrast_ratio(accent_bright_hex, "#161412") >= 4.5


def test_raw_accent_fails_as_text_confirming_the_role_split_is_required(tmp_path, monkeypatch):
    """Documents *why* the surface/text split exists: the raw brand
    orange genuinely fails AA as text-on-bg (this is not a hypothetical
    concern from deb5db0, it's still true of today's token)."""
    assert contrast_ratio("#c44a07", "#0b0b0b") < 4.5


# -- semantic colors: identity/text role + solid badge fill + paired text --

SEMANTIC_ROLES = {
    "success": ("#5fbf7a", "#16301f", "#2f8f52", "#0b0b0b"),
    "warning": ("#e8a93d", "#3a2c12", "#b9791f", "#0b0b0b"),
    "info": ("#5b9bd5", "#122a3a", "#3572b0", "#ffffff"),
    "neutral": ("#9c9890", "#211e1a", "#5b564c", "#ffffff"),
    "danger": ("#e5787c", "#3a1a1c", "#b23a3f", "#ffffff"),
}


def test_semantic_tokens_are_all_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for role, (identity, surface, solid, solid_text) in SEMANTIC_ROLES.items():
        assert f"--cf-{role}: {identity}" in css
        assert f"--cf-{role}-surface: {surface}" in css
        assert f"--cf-{role}-solid: {solid}" in css
        assert f"--cf-{role}-solid-text: {solid_text}" in css


def test_semantic_identity_color_passes_as_text_on_every_surface(tmp_path, monkeypatch):
    surfaces = ["#0b0b0b", "#161412", "#211e1a"]
    for role, (identity, _surface, _solid, _text) in SEMANTIC_ROLES.items():
        for surface_hex in surfaces:
            assert contrast_ratio(identity, surface_hex) >= 4.5, (
                f"{role} identity color fails on {surface_hex}"
            )


def test_semantic_solid_fill_passes_with_its_paired_text(tmp_path, monkeypatch):
    for role, (_identity, _surface, solid, solid_text) in SEMANTIC_ROLES.items():
        assert contrast_ratio(solid_text, solid) >= 4.5, (
            f"{role} solid fill fails with its paired text"
        )


def test_semantic_solid_hover_and_active_preserve_the_paired_text_contrast(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    hover_active = {
        "success": ("#4ea06c", "#63ab7d", "#0b0b0b"),
        "warning": ("#c48d41", "#ca9a57", "#0b0b0b"),
        "info": ("#2d6196", "#285684", "#ffffff"),
        "neutral": ("#4d4941", "#444039", "#ffffff"),
        "danger": ("#973136", "#862c2f", "#ffffff"),
    }
    for role, (hover, active, text) in hover_active.items():
        assert f"--cf-{role}-solid-hover: {hover}" in css
        assert f"--cf-{role}-solid-active: {active}" in css
        assert contrast_ratio(text, hover) >= 4.5
        assert contrast_ratio(text, active) >= 4.5


def test_the_wrong_paired_text_would_have_failed(tmp_path, monkeypatch):
    """Confirms the per-color role split was necessary, not arbitrary --
    success/warning fail with white text; info/neutral/danger fail with
    near-black text, mirroring the brand-orange split."""
    assert contrast_ratio("#ffffff", "#2f8f52") < 4.5   # success solid + white
    assert contrast_ratio("#ffffff", "#b9791f") < 4.5   # warning solid + white
    assert contrast_ratio("#0b0b0b", "#3572b0") < 4.5   # info solid + near-black
    assert contrast_ratio("#0b0b0b", "#5b564c") < 4.5   # neutral solid + near-black
    assert contrast_ratio("#0b0b0b", "#b23a3f") < 4.5   # danger solid + near-black


# -- typography ------------------------------------------------------------

def test_body_uses_the_new_system_font_stack_not_arial(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "font-family: var(--cf-font-sans);" in css
    assert "font-family: Arial, sans-serif;" not in css
    assert '-apple-system, BlinkMacSystemFont, "Segoe UI"' in css


def test_textarea_uses_the_new_mono_stack_token(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "font-family: var(--cf-font-mono);" in css


def test_named_type_scale_is_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for token in (
        "--cf-text-display", "--cf-text-heading", "--cf-text-subheading",
        "--cf-text-body", "--cf-text-small", "--cf-text-label",
        "--cf-text-table-heading", "--cf-text-code",
    ):
        assert token in css


def test_tabular_nums_utility_is_declared_but_not_yet_applied(tmp_path, monkeypatch):
    """Phase 1 declares the hook; wiring it onto real price/count/date
    cells is later-phase work."""
    css = get_root_css(tmp_path, monkeypatch)
    assert ".cf-tabular-nums" in css
    assert "font-variant-numeric: tabular-nums;" in css
    assert 'class="cf-tabular-nums"' not in css


# -- other token categories exist -------------------------------------

def test_spacing_scale_is_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for token in ("--cf-space-1", "--cf-space-4", "--cf-space-8"):
        assert token in css


def test_breakpoints_match_the_requested_ranges(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-bp-compact: 320px" in css
    assert "--cf-bp-tablet: 600px" in css
    assert "--cf-bp-desktop: 1024px" in css
    assert "--cf-bp-wide: 1440px" in css


def test_radii_border_widths_shadows_focus_ring_are_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for token in (
        "--cf-radius-sm", "--cf-radius-md", "--cf-radius-full",
        "--cf-border-width", "--cf-border-width-thick",
        "--cf-shadow-sm", "--cf-shadow-md",
        "--cf-focus-ring", "--cf-focus-ring-width", "--cf-focus-ring-offset",
    ):
        assert token in css


def test_control_heights_and_table_density_are_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for token in (
        "--cf-control-height-sm", "--cf-control-height-md", "--cf-control-height-lg",
        "--cf-table-cell-padding-compact", "--cf-table-cell-padding-comfortable",
    ):
        assert token in css


def test_z_index_layers_are_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    for token in (
        "--cf-z-base", "--cf-z-dropdown", "--cf-z-sticky",
        "--cf-z-overlay", "--cf-z-modal", "--cf-z-toast",
    ):
        assert token in css


def test_motion_durations_are_minimal_and_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-duration-fast: 100ms" in css
    assert "--cf-duration-base: 150ms" in css


def test_disabled_loading_and_selected_states_are_declared(tmp_path, monkeypatch):
    css = get_root_css(tmp_path, monkeypatch)
    assert "--cf-disabled-opacity" in css
    assert "--cf-loading-opacity" in css
    assert "--cf-selected-bg" in css
    assert "--cf-selected-text" in css


# -- status-semantics mapping stub (Python side) -------------------------

def test_status_semantic_roles_stub_exists_and_is_empty():
    assert hasattr(main, "STATUS_SEMANTIC_ROLES")
    assert main.STATUS_SEMANTIC_ROLES == {}


# -- scope discipline: nothing new should be visually wired up yet -----

def test_no_page_references_a_brand_new_semantic_css_variable_yet(tmp_path, monkeypatch):
    """This phase declares tokens; it does not wire them into any rule
    beyond what was already variable-driven (colors) or explicitly
    instructed (the font stack). A semantic color appearing in a var()
    reference anywhere -- inside :root's own declarations or any other
    rule -- would mean something already consumed it, which isn't the
    goal yet."""
    css = get_root_css(tmp_path, monkeypatch)
    for role in ("success", "warning", "info", "neutral", "danger"):
        assert f"var(--cf-{role}" not in css
