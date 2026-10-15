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

def test_only_a_direct_child_of_body_is_sticky(css):
    """★ THE SELECTOR IS THE WHOLE FIX (v2.51.0).

    This rule first shipped against the BARE `nav` type selector, and
    every <nav> in the app matched it: the breadcrumbs, the Orders status
    tabs, the pick wave's tab bar, and its "Batches" link index. All of
    them pinned at top: 0 with the same z-index, and at equal z-index the
    later element in the DOM paints on top -- so the LAST nav on a page
    sat over the site nav. The operator reported the batch index covering
    the bar he was trying to use.

    INVERSION RECORDED: the test here used to assert exactly that bare
    selector, which is why it could not catch this. It asserts the scoped
    one now, and the rendered-page tests below are what actually hold the
    line."""
    desktop = media_block(css, "min-width: 1024px")
    assert re.search(r"body\s*>\s*nav\s*\{[^}]*position:\s*sticky", desktop), (
        "the sticky rule must be scoped to a direct child of body"
    )
    assert re.search(r"body\s*>\s*nav\s*\{[^}]*top:\s*0", desktop)


def test_no_bare_nav_selector_makes_every_nav_sticky(css):
    """The specific regression. A bare `nav { position: sticky }` anywhere
    in the stylesheet brings the bug straight back."""
    for match in re.finditer(r"([^{};]+)\{([^{}]*position:\s*sticky[^{}]*)\}", css):
        selector = " ".join(match.group(1).split()).split("*/")[-1].strip()
        assert selector != "nav", (
            "a bare `nav` selector is sticky again -- scope it to body > nav"
        )


def test_the_nav_is_not_sticky_outside_that_media_query(css):
    """Below 1024px the mobile menu expands into a full-width column of
    eight links; pinning that to a phone viewport would cover the screen.

    The base rule must therefore stay static. Checked by cutting the
    desktop block out and confirming nothing left makes any nav sticky."""
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
    assert re.search(r"body\s*>\s*nav\s*\{[^}]*z-index:\s*var\(--cf-z-nav\)", desktop)

    def token(name):
        return int(re.search(rf"--cf-z-{name}:\s*(\d+)", css).group(1))

    assert token("nav") > token("sticky"), "the nav must paint above the toolbars"
    assert token("nav") < token("overlay"), "but below overlays and modals"


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


# --- the outcome, on really rendered pages ------------------------------
#
# The selector assertions above are necessary but were not sufficient:
# the version of this file that shipped with v2.46.0 asserted the BARE
# `nav` selector and passed happily while four navs per page fought over
# top: 0. These tests parse real pages instead and count how many navs the
# shipped selector can actually match.

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


class NavParser(HTMLParser):
    """Every <nav> on the page, with the tag that encloses it.

    A real stack-tracking parse rather than a regex: the question is a
    PARENT relationship, which is exactly what a regex cannot see, and
    getting that wrong is how the original bug survived review.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.navs = []

    def handle_starttag(self, tag, attrs):
        if tag == "nav":
            self.navs.append({
                "parent": self.stack[-1] if self.stack else None,
                "class": dict(attrs).get("class", ""),
            })
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag == "nav":
            self.navs.append({
                "parent": self.stack[-1] if self.stack else None,
                "class": dict(attrs).get("class", ""),
            })

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass


def navs_of(html):
    parser = NavParser()
    parser.feed(html)
    return parser.navs


def sticky_navs(html):
    """The navs the shipped `body > nav` selector would actually match."""
    return [nav for nav in navs_of(html) if nav["parent"] == "body"]


@pytest.fixture
def page_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'sticky-pages.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def seed_wave(engine):
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        wave = PickWave(label="W1", status="active")
        session.add_all([batch, wave])
        session.flush()
        order = SalesOrder(
            external_order_id="o-1", external_label="O-1",
            source="manapool", status="ready_to_pick",
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id, name="Fellwar Stone", quantity=1,
            language_id="EN", condition_id="NM", finish_id="NF",
        )
        session.add(item)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Fellwar Stone", set_code="soc",
            collector_number="347", scryfall_id="sf-1", condition="Near Mint",
            condition_id="NM", finish="normal", finish_id="NF",
            language_id="EN", status="reserved", current_price=1.0,
        )
        session.add(card)
        session.flush()
        session.add_all([
            PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                           batch_id=batch.id, status="allocated"),
            PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"),
        ])
        session.commit()
        return wave.id


def test_orders_has_several_navs_and_only_the_site_nav_is_sticky(page_db):
    """/orders renders the site nav, the breadcrumbs and the status tabs.
    Before the fix all three pinned at top: 0 and the status tabs -- last
    in the DOM -- covered the site nav."""
    html = TestClient(main.app).get("/orders").text
    all_navs = navs_of(html)
    assert len(all_navs) >= 3, [nav["class"] for nav in all_navs]
    classes = {nav["class"] for nav in all_navs}
    assert any("status-tabs" in c for c in classes), classes
    assert any("breadcrumbs" in c for c in classes), classes

    sticky = sticky_navs(html)
    assert len(sticky) == 1, (
        f"expected exactly one sticky nav, got {[n['class'] for n in sticky]}"
    )
    assert sticky[0]["class"] == "", "the sticky one must be the site nav"


def test_the_pick_wave_page_only_makes_the_site_nav_sticky(page_db):
    """★ THE PAGE THE OPERATOR REPORTED. Its "Batches" link index is a
    <nav class="batch-index">, and it was the element pinned at the top of
    the viewport, covering the bar he was trying to use."""
    wave_id = seed_wave(page_db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=picklist").text
    classes = {nav["class"] for nav in navs_of(html)}
    assert any("batch-index" in c for c in classes), classes

    sticky = sticky_navs(html)
    assert len(sticky) == 1, (
        f"expected exactly one sticky nav, got {[n['class'] for n in sticky]}"
    )
    assert sticky[0]["class"] == ""


def test_the_batch_index_is_not_sticky(page_db):
    """It goes back to what it was before the sticky nav shipped. Making
    it sticky BELOW the nav would be a feature, and was not asked for."""
    wave_id = seed_wave(page_db)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=picklist").text
    for nav in navs_of(html):
        if "batch-index" in nav["class"]:
            assert nav["parent"] != "body", (
                "the batch index must not match body > nav"
            )
            break
    else:
        pytest.fail("no batch index on the page")


def test_the_consignor_portal_nav_stays_sticky(page_db):
    """The portal has its own minimal shell, and v2.46.0 deliberately gave
    it the sticky nav too. It is also a direct child of body, so scoping
    the selector must not quietly drop it -- which is why `nav:has(>
    .nav-bar)` was rejected as the fix."""
    html = TestClient(main.app).get("/portal/login").text
    sticky = sticky_navs(html)
    assert len(sticky) == 1, [nav["class"] for nav in sticky]


def test_the_stylesheet_contains_no_literal_nav_tag(css):
    """★ THE SAME TRAP AS THE DATED COMMENT, in a new costume.

    The stylesheet is served INLINE in every page, so anything written in
    a CSS comment lands in the HTML of every page. A dated comment put a
    year into every page and broke a portal test; the comment written for
    this fix spelled the tag name in angle brackets and broke
    test_admin_nav, whose helper slices a page from its first nav tag --
    it started inside the comment instead of at the real element.

    Scoped to the nav tag deliberately rather than all markup: several
    older comments name other elements in brackets and collide with
    nothing, so a blanket ban would be noise. This is the one tag a
    page-scanning helper keys on.
    """
    assert "<nav" not in css, (
        "a literal nav tag appears in the stylesheet, which ships inside "
        "every page -- write the element name without angle brackets"
    )
