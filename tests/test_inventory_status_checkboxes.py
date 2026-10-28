"""Inventory Search: the Status filter is checkboxes, not a dropdown
(operator decision, 2026-10-27).

Plain HTML, no JavaScript, repeated `status=` parameters.

★ THE WHOLE DIFFICULTY IS THAT AN UNTICKED CHECKBOX SENDS NOTHING.
"Submitted with every box cleared" and "arrived at /inventory fresh"
produce byte-identical requests, and they must give OPPOSITE answers:
cleared means show everything, fresh means show Listed only. A hidden
marker field that always submits with the form is what separates them,
and these tests pin both sides of it -- including that paging and
sorting carry the selection, which is where a reset to the default would
show up first.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_status_filter_service as svc
import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, InventoryListingStatus


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'statusboxes.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    with Session(engine) as s:
        s.add(Batch(id=1, batch_code="A1"))
        # One card per status the page offers, so every box has something
        # to find and nothing can pass by accident.
        s.add_all([
            InventoryCard(id=1, batch_id=1, name="Listed Card", status="available"),
            InventoryCard(id=2, batch_id=1, name="Unlisted Card", status="available"),
            InventoryCard(id=3, batch_id=1, name="Reserved Card", status="reserved"),
            InventoryCard(id=4, batch_id=1, name="Sold Card", status="sold"),
            InventoryCard(id=5, batch_id=1, name="Unavailable Card", status="unsellable"),
            InventoryCard(id=6, batch_id=1, name="Removed Card", status="removed"),
        ])
        s.add(InventoryListingStatus(inventory_card_id=1, listing_status="listed"))
        # Page size is restricted to (25, 50, 100), so paging needs more
        # than 25 rows to exist at all. These filler cards are sold and
        # removed so they only ever appear under those two boxes.
        for n in range(30):
            s.add(InventoryCard(id=100 + n, batch_id=1, name=f"ZZ Filler {n:02d}",
                                status="sold" if n % 2 else "removed"))
        s.commit()
    return engine


ALL_NAMES = {"Listed Card", "Unlisted Card", "Reserved Card",
             "Sold Card", "Unavailable Card", "Removed Card"}


def get(url):
    response = TestClient(main.app).get(url)
    assert response.status_code == 200, url
    return response.text


def names(html):
    """Which cards are listed as ROWS. Keyed on the per-row select
    checkbox's aria-label, which exists once per rendered row -- the
    name also appears in link text and titles, so a bare substring
    would count a card that is merely mentioned."""
    return {n for n in ALL_NAMES if f'aria-label="Select {n}"' in html}


def ticked(html):
    """Which status boxes are rendered checked."""
    row = html[html.index('class="filter-checkbox-row"'):]
    row = row[:row.index("</div>")]
    return {
        m.group(1) for m in
        re.finditer(r'name="status" value="([a-z_]+)" checked', row)
    }


def offered(html):
    row = html[html.index('class="filter-checkbox-row"'):]
    row = row[:row.index("</div>")]
    return re.findall(r'name="status" value="([a-z_]+)"', row)


# ---------------------------------------------------------------------
# (a) bare /inventory -> Listed only
# ---------------------------------------------------------------------

def test_a_bare_page_shows_only_listed_cards(db):
    html = get("/inventory")
    assert names(html) == {"Listed Card"}
    assert ticked(html) == {"listed"}


def test_every_status_gets_a_box_in_the_operators_order(db):
    html = get("/inventory")
    assert offered(html) == ["listed", "not_listed", "reserved",
                             "sold", "unsellable", "removed"]


def test_the_dropdown_is_gone(db):
    html = get("/inventory")
    assert '<select id="inv-status"' not in html
    assert 'name="status" value="listed"' in html


# ---------------------------------------------------------------------
# (b) an explicit selection -> exactly those, ticked
# ---------------------------------------------------------------------

def test_two_statuses_show_exactly_those_and_are_both_ticked(db):
    html = get("/inventory?status=sold&status=removed")
    assert names(html) == {"Sold Card", "Removed Card"}
    assert ticked(html) == {"sold", "removed"}


@pytest.mark.parametrize("value,expected", [
    ("listed", "Listed Card"),
    ("not_listed", "Unlisted Card"),
    ("reserved", "Reserved Card"),
    ("sold", "Sold Card"),
    ("unsellable", "Unavailable Card"),
    ("removed", "Removed Card"),
])
def test_each_box_on_its_own_finds_its_own_card(db, value, expected):
    html = get(f"/inventory?status={value}")
    assert names(html) == {expected}
    assert ticked(html) == {value}


def test_listed_and_not_listed_together_are_every_available_card(db):
    """Neither is a stored status: both are available cards, split by
    whether a listing exists, so together they are exactly `available`."""
    html = get("/inventory?status=listed&status=not_listed")
    assert names(html) == {"Listed Card", "Unlisted Card"}


def test_a_mixed_selection_ORs_rather_than_ANDs(db):
    """★ A card has ONE status, so ANDing two would return nothing."""
    html = get("/inventory?status=listed&status=sold")
    assert names(html) == {"Listed Card", "Sold Card"}


def test_every_box_ticked_shows_everything(db):
    html = get("/inventory?" + "&".join(f"status={v}" for v in svc.VALID_STATUSES))
    assert names(html) == ALL_NAMES


# ---------------------------------------------------------------------
# (c) submitted with nothing ticked -> all statuses
# ---------------------------------------------------------------------

def test_clearing_every_box_shows_all_statuses(db):
    html = get(f"/inventory?{svc.SUBMITTED_MARKER}=1")
    assert names(html) == ALL_NAMES
    assert ticked(html) == set()


def test_clearing_every_box_never_shows_an_empty_page(db):
    html = get(f"/inventory?{svc.SUBMITTED_MARKER}=1")
    assert "No cards found." not in html


def test_the_marker_is_rendered_so_the_form_can_submit_it(db):
    html = get("/inventory")
    assert f'<input type="hidden" name="{svc.SUBMITTED_MARKER}" value="1">' in html


def test_a_named_status_beats_the_marker(db):
    """A link carrying status= means exactly that, whether or not it also
    came from the form."""
    html = get(f"/inventory?{svc.SUBMITTED_MARKER}=1&status=sold")
    assert names(html) == {"Sold Card"}
    assert ticked(html) == {"sold"}


# ---------------------------------------------------------------------
# (d) paging and sorting keep the selection
# ---------------------------------------------------------------------

def follow(html, pattern):
    match = re.search(pattern, html)
    assert match, pattern
    return match.group(0).replace("&amp;", "&")


def test_the_sort_links_carry_the_selection(db):
    html = get("/inventory?status=sold&status=removed")
    link = follow(html, r'/inventory\?[^"]*sort=[a-z_]+[^"]*')
    assert "status=sold" in link and "status=removed" in link
    # Following it keeps the selection ticked and never shows a card
    # outside it. The exact rows depend on the sort direction and the
    # filler, so the assertion is on the selection, not the page.
    followed = get(link)
    assert ticked(followed) == {"sold", "removed"}
    assert "Listed Card" not in followed.replace("Select Listed Card", "")


def test_sorting_does_not_reset_the_selection_to_listed(db):
    html = get("/inventory?status=sold&direction=desc&sort=name")
    assert ticked(html) == {"sold"}
    assert names(html) == {"Sold Card"}


def test_page_two_keeps_the_selection(db):
    html = get("/inventory?status=sold&status=removed&page_size=25")
    link = follow(html, r'/inventory\?[^"]*page=2[^"]*')
    assert "status=sold" in link and "status=removed" in link
    second = get(link)
    assert ticked(second) == {"sold", "removed"}
    assert "Listed Card" not in second.replace("Select Listed Card", "")


def test_page_two_of_a_cleared_selection_stays_cleared(db):
    """★ THE CASE THE MARKER EXISTS FOR. Without it the page-2 link would
    be a bare /inventory and silently put the operator back on Listed."""
    html = get(f"/inventory?{svc.SUBMITTED_MARKER}=1&page_size=25")
    link = follow(html, r'/inventory\?[^"]*page=2[^"]*')
    assert svc.SUBMITTED_MARKER in link, link
    second = get(link)
    assert ticked(second) == set(), "page 2 must not revert to Listed"


# ---------------------------------------------------------------------
# (e) the links that pass status=, and a stale one
# ---------------------------------------------------------------------

def test_an_exception_state_link_combined_with_a_status_still_filters(db):
    """/inventory?exception_status=... is the one in-app link that is
    documented alongside a status; both must still apply together."""
    html = get("/inventory?exception_status=exception_unresolved&status=removed")
    assert names(html) == set(), "no unresolved exceptions in this fixture"
    assert ticked(html) == {"removed"}


def test_a_stale_link_naming_an_unknown_status_shows_everything(db):
    """`status=available` was never a valid filter value and used to fall
    through to "all". A bookmark carrying it must not silently become the
    Listed default."""
    html = get("/inventory?status=available")
    assert names(html) == ALL_NAMES
    assert ticked(html) == set()


def test_an_unknown_status_mixed_with_a_good_one_keeps_the_good_one(db):
    html = get("/inventory?status=available&status=sold")
    assert names(html) == {"Sold Card"}
    assert ticked(html) == {"sold"}


# ---------------------------------------------------------------------
# No JavaScript, and the query count
# ---------------------------------------------------------------------

def test_no_javascript_was_added_for_the_checkboxes(db):
    html = get("/inventory")
    row_start = html.index('class="filter-checkbox-row"')
    row = html[row_start:row_start + 2000]
    assert "<script" not in row
    assert "onchange=" not in row and "onclick=" not in row


def test_the_page_costs_the_same_number_of_statements_whatever_is_ticked(db):
    """Pinned so a future selection cannot quietly become a query per
    ticked box."""
    from sqlalchemy import event

    def count(url):
        seen = []
        def before(conn, cursor, statement, *a):
            seen.append(statement)
        event.listen(db, "before_cursor_execute", before)
        try:
            get(url)
        finally:
            event.remove(db, "before_cursor_execute", before)
        return len(seen)

    one = count("/inventory?status=sold")
    six = count("/inventory?" + "&".join(f"status={v}" for v in svc.VALID_STATUSES))
    cleared = count(f"/inventory?{svc.SUBMITTED_MARKER}=1&status_submitted=1")
    assert one == six == cleared, (one, six, cleared)


def test_calling_the_route_directly_without_a_status_still_works(db):
    """★ FOUND BY A LIVE CHECK, not by a test. Over HTTP FastAPI resolves
    the parameter into a list, but a direct call -- the pattern several
    tests and diagnostics already use -- leaves its Query default object
    in place, which is not iterable. It raised TypeError and took the
    whole page down."""
    html = main.inventory_search(show_all=True)
    assert "Inventory Search" in html
    assert 'name="status" value="listed" checked' in html


def test_calling_the_route_directly_with_a_bare_string_filters_on_it(db):
    html = main.inventory_search(show_all=True, status="sold")
    assert 'aria-label="Select Sold Card"' in html
    assert 'aria-label="Select Listed Card"' not in html
