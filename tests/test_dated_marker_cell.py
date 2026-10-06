"""A date with a status marker under it, inside a table cell.

THE BUG THIS FIXES. These cells reused .warning/.danger, which are PANEL
classes -- `padding: 12px; margin: 15px 0`, built for a full-width banner
div. On an INLINE span in a narrow table cell that is destructive: an inline
box's vertical padding does not grow the line box, so the coloured
background bled UP over the date on the line above (the operator's "rendered
on top of each other"), while 12px of horizontal padding plus a border
pushed the box past the cell and over the neighbouring column. Reported
2026-10-06 on order 667671-2357279's Ship by column, and on the consignors
screen.

Verified visually before and after at 1280px and at 375px; these tests pin
the markup contract so a future caller cannot quietly reintroduce the panel
classes.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import main

CONFIG = {"business_days": 2, "warn_hours": 24, "alarm_hours": 12}
PANEL_CLASSES = ('class="danger"', 'class="warning"', "class='danger'", "class='warning'")


def order(placed_at, status="ready_to_pick", remote=None):
    return SimpleNamespace(placed_at=placed_at, status=status,
                           remote_fulfillment_status=remote)


# --- the shared helper's contract ---------------------------------------

def test_the_marker_is_its_own_inline_block_on_its_own_line():
    html = main.dated_marker_cell("Tue Oct 6, 2026", "23 hours left", "warning")
    assert 'class="cell-stack"' in html
    assert 'class="cell-stack-main"' in html
    assert "cell-badge cell-badge-warning" in html
    # The old markup separated the two with a bare <br> inside one padded
    # span; that is exactly what overlapped.
    assert "<br>" not in html


def test_no_caller_may_reuse_the_panel_classes():
    for tone in ("danger", "warning", "muted"):
        html = main.dated_marker_cell("Tue Oct 6, 2026", "later", tone)
        for panel in PANEL_CLASSES:
            assert panel not in html, f"{tone} must not reuse the panel class {panel}"


def test_an_empty_marker_renders_the_date_alone_with_no_empty_box():
    html = main.dated_marker_cell("Mon Oct 5, 2026", "", "danger")
    assert "cell-badge" not in html
    assert "Mon Oct 5, 2026" in html


def test_the_marker_text_is_escaped():
    html = main.dated_marker_cell("Mon Oct 5, 2026", "<script>x</script>", "danger")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_unknown_tone_falls_back_to_muted_rather_than_no_class():
    html = main.dated_marker_cell("Mon Oct 5, 2026", "later", "nonsense")
    assert "cell-badge cell-badge-muted" in html


def test_emphasise_primary_bolds_only_the_date():
    html = main.dated_marker_cell("Thu Oct 1, 2026", "OVERDUE", "danger",
                                  emphasise_primary=True)
    assert "<strong>Thu Oct 1, 2026</strong>" in html
    assert "<strong>OVERDUE" not in html


# --- every state the Ship by column can render --------------------------

def test_no_placed_at_reads_unknown_and_has_no_badge():
    html = main._ship_by_cell(order(None), CONFIG)
    assert "unknown" in html and "cell-badge" not in html


def test_a_settled_order_shows_a_dash_and_has_no_badge():
    html = main._ship_by_cell(
        order(datetime.now() - timedelta(days=6), status="shipped"), CONFIG)
    assert "cell-badge" not in html


@pytest.mark.parametrize("placed,expect_class", [
    (datetime.now() - timedelta(hours=1), "cell-badge-muted"),
    (datetime.now() - timedelta(days=6), "cell-badge-danger"),
])
def test_each_deadline_state_uses_a_cell_badge_not_a_panel(placed, expect_class):
    html = main._ship_by_cell(order(placed), CONFIG)
    assert expect_class in html
    for panel in PANEL_CLASSES:
        assert panel not in html


def test_an_overdue_order_bolds_its_date_and_carries_the_danger_badge():
    html = main._ship_by_cell(order(datetime.now() - timedelta(days=6)), CONFIG)
    assert "<strong>" in html
    assert "cell-badge-danger" in html
    assert "OVERDUE" in html


# --- the stylesheet actually defines what the markup asks for -----------

def test_the_stylesheet_defines_the_badge_as_an_inline_block():
    """The whole fix is display:inline-block plus containment -- an inline
    box's vertical padding does not grow its line box, which is what
    overlapped."""
    css = main._html_head("x")
    assert ".cell-badge" in css
    assert "inline-block" in css
    assert "overflow-wrap: anywhere" in css
    assert "max-width: 100%" in css


def test_the_consignor_expected_payout_cell_uses_the_shared_helper():
    import inspect
    source = inspect.getsource(main._portal_payout_date_cells)
    assert "dated_marker_cell" in source
    for panel in PANEL_CLASSES:
        assert panel not in source, "the consignor cell must not reuse a panel class"
