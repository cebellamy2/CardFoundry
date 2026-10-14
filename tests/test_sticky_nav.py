"""The top nav stays in view while a long page scrolls.

CSS only, no JavaScript, matching the app's standing rule.

★ MEASURED, NOT COMPUTED. The nav's height is not a constant: `.nav-bar`
and `.nav-links` both set `flex-wrap: wrap`, so the eight links sit on one
row at wide widths and TWO rows when space runs out. Measured live in
Chromium against the real nav markup on /inventory, 2026-10-14:

    1024px -> 104px, two link rows
    1100px -> 104px, two link rows
    1150px ->  74px, one link row
    1280px ->  74px, one link row
    1440px ->  74px, one link row

With the widest Attention badge seen in production (three digits -- the
count has reached 161) the nav needs a 1145px viewport to fit one row, and
the body's own 1200px max-width caps its usable width, so the geometry is
frozen for every viewport at or above 1248px. Hence two token values.

`flex-wrap: nowrap` was tried first and REJECTED: it does hold the nav at
74px, but it pushed the page into real horizontal overflow at 1024px
(document scrollWidth 1092 vs clientWidth 1009) -- exactly what the
site-wide overflow sweep of v1.84.1 existed to remove.

The live behaviour these tests stand in for was verified in a real browser
at four widths, since no assertion here can prove a pixel:

    1280px: nav position sticky, stuck at top 0 after scrolling,
            --cf-nav-height 74px, .bulk-toolbar top 74px,
            #main-content scroll-margin-top 90px, no horizontal overflow
    1024px: nav 104px, sticky and stuck, token 104px,
            .bulk-toolbar top 104px, scroll-margin-top 120px,
            no horizontal overflow
     800px: nav position STATIC, .bulk-toolbar top 0px,
            scroll-margin-top 0px, token unset -- all correct below the
            breakpoint
"""
import re

import pytest

import main


@pytest.fixture(scope="module")
def css():
    """The real served stylesheet, from the real page head."""
    head = main._html_head("Sticky nav test")
    return head[head.index("<style>"):head.index("</style>")]


def media_block(css, condition):
    """The body of the first @media block matching `condition`.

    Brace-counted rather than regexed: the block contains nested rules, and
    a non-greedy match would stop at the first inner closing brace.
    """
    start = css.index(f"@media ({condition})")
    open_brace = css.index("{", start)
    depth = 0
    for i in range(open_brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1:i]
    raise AssertionError(f"unbalanced braces after @media ({condition})")


# --- the nav itself ------------------------------------------------------

def test_the_nav_is_sticky_only_from_1024px(css):
    desktop = media_block(css, "min-width: 1024px")
    assert "position: sticky" in desktop
    assert re.search(r"\bnav\s*\{[^}]*position:\s*sticky", desktop), (
        "the sticky rule must be on nav itself"
    )
    assert re.search(r"\bnav\s*\{[^}]*top:\s*0", desktop)


def test_the_nav_is_not_sticky_outside_that_media_query(css):
    """Below 1024px the mobile menu expands into a full-width column of
    eight links; pinning that to a phone viewport would cover the screen.

    The base `nav { }` rule must therefore stay static. Checked by cutting
    the desktop block out and confirming nothing left makes nav sticky."""
    desktop_start = css.index("@media (min-width: 1024px)")
    base = css[:desktop_start]
    nav_rules = re.findall(r"\bnav\s*\{([^}]*)\}", base)
    assert nav_rules, "expected a base nav rule to exist"
    for body in nav_rules:
        assert "position:" not in body, (
            f"the base nav rule must not set position; found: {body.strip()}"
        )


def test_the_nav_sits_above_the_sticky_toolbars_it_offsets(css):
    """Both were --cf-z-sticky at first and the nav LOST: equal z-index
    means the later element in the DOM paints on top, and every toolbar
    comes after the nav. So the nav needs its own higher token."""
    desktop = media_block(css, "min-width: 1024px")
    assert re.search(r"\bnav\s*\{[^}]*z-index:\s*var\(--cf-z-nav\)", desktop)

    def token(name):
        return int(re.search(rf"--cf-z-{name}:\s*(\d+)", css).group(1))

    assert token("nav") > token("sticky"), "the nav must paint above the toolbars"
    assert token("nav") < token("overlay"), "but below overlays and modals"


# --- the two measured heights -------------------------------------------

def test_the_nav_height_token_is_the_two_row_height_from_1024px(css):
    assert "--cf-nav-height: 104px" in media_block(css, "min-width: 1024px")


def test_the_nav_height_token_drops_to_the_one_row_height_from_1200px(css):
    assert "--cf-nav-height: 74px" in media_block(css, "min-width: 1200px")


def test_the_one_row_override_comes_after_the_two_row_value(css):
    """Both are `:root` at the same specificity, so the 1200px value only
    wins by source order. Reversed, every wide viewport would reserve the
    two-row height."""
    assert css.index("@media (min-width: 1024px)") < css.index("@media (min-width: 1200px)")


def test_the_token_is_never_defined_outside_the_sticky_media_queries(css):
    """It has no meaning below 1024px -- the nav is static there and
    nothing offsets against it. A base value would be a number waiting to
    be used by mistake."""
    desktop_start = css.index("@media (min-width: 1024px)")
    assert "--cf-nav-height" not in css[:desktop_start]


# --- the four elements that were already sticky at top: 0 ---------------

ALREADY_STICKY = ("bulk-toolbar", "bulk-toolbar-stack", "wave-summary-sticky")


@pytest.mark.parametrize("selector", ALREADY_STICKY)
def test_each_already_sticky_element_is_re_offset_below_the_nav(css, selector):
    """Each of these was `position: sticky; top: 0` before this change and
    would otherwise pin itself UNDERNEATH the sticky nav.

    There is a recorded incident for exactly this class of bug one level
    down: .bulk-toolbar-stack exists because two independently-sticky
    toolbars stuck to the same offset and the later one covered the
    earlier one."""
    desktop = media_block(css, "min-width: 1024px")
    group = re.search(
        r"((?:\.[\w-]+,\s*)*\.[\w-]*" + re.escape(selector) + r"(?:,\s*\.[\w-]+)*)\s*\{([^}]*)\}",
        desktop,
    )
    assert group, f".{selector} is not re-offset inside the desktop media query"
    assert "top: var(--cf-nav-height)" in group.group(2)


def test_the_chute_compare_frame_keeps_its_own_extra_gap(css):
    """It was `top: var(--cf-space-4)`, not `top: 0` -- a deliberate gap so
    the card image is not flush against the viewport edge. That gap is
    preserved on top of the nav height rather than replaced by it."""
    desktop = media_block(css, "min-width: 1024px")
    rule = re.search(r"\.chute-compare-frame\s*\{([^}]*)\}", desktop)
    assert rule, ".chute-compare-frame is not re-offset"
    assert "calc(var(--cf-nav-height) + var(--cf-space-4))" in rule.group(1)


def test_the_re_offsets_come_after_the_rules_they_override(css):
    """All of this is one inline stylesheet at equal specificity, so the
    desktop block only wins on source order. If it moved above the base
    `top: 0` rules the offsets would silently stop applying."""
    desktop_start = css.index("@media (min-width: 1024px)")
    base = css[:desktop_start]
    for selector in ALREADY_STICKY:
        assert f".{selector} {{" in base, (
            f"expected .{selector}'s base rule to exist before the override"
        )


# --- anchor targets ------------------------------------------------------

def test_anchor_targets_get_a_scroll_margin_so_they_clear_the_bar(css):
    """THE LIVE CASE: the wave page's tracking notice links to a specific
    order row by anchor (`#wave-order-<id>`, shipped v2.45.0). Without
    this the row scrolls to the very top of the viewport and lands behind
    the sticky nav -- the feature shipped three releases ago would quietly
    break.

    There was no scroll-margin or scroll-padding rule anywhere in the
    stylesheet before this change."""
    desktop = media_block(css, "min-width: 1024px")
    rule = re.search(r"\[id\]\s*\{([^}]*)\}", desktop)
    assert rule, "no [id] scroll-margin rule"
    assert "scroll-margin-top" in rule.group(1)
    assert "calc(var(--cf-nav-height) + var(--cf-space-4))" in rule.group(1)


def test_the_anchor_rule_covers_the_wave_order_row_it_exists_for(css):
    """`[id]` rather than a named list, so every present and future anchor
    target is covered. This pins that the actual target still carries an
    id the rule can match."""
    source = open(main.__file__).read()
    assert 'id="wave-order-{order.id}"' in source, (
        "the anchor target this rule exists for has moved or been renamed"
    )
    desktop = media_block(css, "min-width: 1024px")
    assert re.search(r"\[id\]\s*\{", desktop)


def test_scroll_margin_is_not_applied_below_the_breakpoint(css):
    """The nav is static there, so a scroll margin would push anchor
    targets down for no reason."""
    desktop_start = css.index("@media (min-width: 1024px)")
    assert "scroll-margin" not in css[:desktop_start]


# --- the trap this change fell into, now a standing guard ---------------

def test_the_served_stylesheet_contains_no_four_digit_year(css):
    """★ THIS HAS BITTEN TWICE. The whole stylesheet is served INLINE in
    every page's <style>, so a date written in a CSS comment appears in the
    HTML of every page in the app.

    First time: a dated comment put a year into every page and tripped an
    unrelated test that scanned a table row for one. Second time: the
    sticky-nav comment added here did exactly the same thing and broke
    test_portal_row_is_blank_for_an_unsold_card, which asserts no year
    appears in a portal row.

    So: no dates, no version years, nothing matching 19xx/20xx anywhere in
    the stylesheet. Put the dates, measurements and incident history in a
    Python comment or a test docstring -- neither is served to the browser.
    A pre-existing date in the brand-mark comment was removed to make this
    enforceable; its substance now points at the CHANGELOG instead.
    """
    import re as _re
    years = _re.findall(r"\b(?:19|20)\d{2}\b", css)
    assert not years, (
        f"{len(years)} four-digit year(s) in the served stylesheet: {sorted(set(years))}. "
        "Move the prose into Python or a test docstring."
    )
