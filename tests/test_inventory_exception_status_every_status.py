"""An exception-state search shows exception cards of EVERY status
(operator decision, 2026-10-28).

★ WHY THIS FILTER NEEDS ITS OWN RULE. Since v2.73.0 a bare /inventory
shows Listed only, which is right for the job the operator opens the
page for. The exception-state filter is the opposite job: marking a
fulfilment exception is what moved the card to removed or unsellable in
the first place, so a Listed default hid very nearly every row an
exception search exists to find. Applying that filter and naming no
status now means no status filter at all -- and the boxes render
un-ticked, so the page is honest about it.

Naming a status still wins: ?exception_status=...&status=removed is an
explicit search and keeps filtering.

Fixtures go through the real mark_fulfillment_exception, so the cards
reach the statuses production puts them in rather than ones chosen to
make the test pass.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_status_filter_service as svc
import inventory_sync_service
import main
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, Batch, InventoryCard, InventoryListingStatus
from tests.test_fulfillment_exception_service import seed


REMOVED_CARD = "Missing Mel"
UNSELLABLE_CARD = "Mismatched Mo"
LISTED_CARD = "Listed Lou"
ALL_NAMES = {REMOVED_CARD, UNSELLABLE_CARD, LISTED_CARD}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'exception-every-status.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    with Session(engine) as session:
        # Two unresolved exceptions through the real service: "missing"
        # leaves the card removed, "inventory_mismatch" leaves it
        # unsellable. Neither status is ticked by default, which is the
        # whole point of this file.
        _, missing_item, missing_card, missing_allocation = seed(session)
        _, mismatch_item, mismatch_card, mismatch_allocation = seed(session)
        # Both sides of the name, not just the card: marking an exception
        # crosschecks the order item's identity against the physical
        # card's and refuses when they disagree.
        missing_card.name = missing_item.name = REMOVED_CARD
        mismatch_card.name = mismatch_item.name = UNSELLABLE_CARD
        session.flush()
        mark_fulfillment_exception(session, missing_allocation.id, "missing", "Not found")
        mark_fulfillment_exception(
            session, mismatch_allocation.id, "inventory_mismatch", "Wrong printing",
        )
        # One ordinary listed card with no exception at all, so "shows
        # every status" cannot be confused with "shows everything".
        batch = Batch(batch_code="LISTED-1", is_archived=False)
        session.add(batch); session.flush()
        listed = InventoryCard(batch_id=batch.id, name=LISTED_CARD, status="available")
        session.add(listed); session.flush()
        session.add(InventoryListingStatus(
            inventory_card_id=listed.id, listing_status="listed",
        ))
        session.commit()
        assert {missing_card.status, mismatch_card.status} == {"removed", "unsellable"}
    return engine


def get(url):
    response = TestClient(main.app).get(url)
    assert response.status_code == 200, url
    return response.text


def names(html):
    """Which cards are listed as ROWS -- keyed on the per-row select
    checkbox, which exists once per rendered row. A bare substring would
    also count a card merely mentioned in link text or a title."""
    return {n for n in ALL_NAMES if f'aria-label="Select {n}"' in html}


def ticked(html):
    row = html[html.index('class="filter-checkbox-row"'):]
    row = row[:row.index("</div>")]
    return {
        m.group(1) for m in
        re.finditer(r'value="([a-z_]+)"[^>]*checked', row)
    }


EXCEPTION_SEARCH = "exception_status=exception_unresolved"


# ---------------------------------------------------------------------
# the page that produces the search, and what the search shows
# ---------------------------------------------------------------------

def test_the_filter_form_is_what_produces_an_exception_search(db):
    """★ NO PAGE IN THE APP EMITS THIS AS A LINK -- the filter form's own
    Exception state dropdown is the only in-app producer, so that is the
    thing to follow from."""
    html = get("/inventory")
    assert 'name="exception_status"' in html
    assert 'value="exception_unresolved"' in html


def test_an_exception_search_shows_removed_and_unsellable_cards(db):
    """The form submits the hidden marker with every search."""
    html = get(f"/inventory?{EXCEPTION_SEARCH}&{svc.SUBMITTED_MARKER}=1")
    assert names(html) == {REMOVED_CARD, UNSELLABLE_CARD}
    assert ticked(html) == set(), "the boxes must be honest: no status filter"


def test_an_exception_search_without_the_marker_shows_them_too(db):
    """A bookmark or a hand-typed URL carries no marker. It must not fall
    through to the Listed default either."""
    html = get(f"/inventory?{EXCEPTION_SEARCH}")
    assert names(html) == {REMOVED_CARD, UNSELLABLE_CARD}
    assert ticked(html) == set()


def test_an_exception_search_is_not_the_same_as_showing_everything(db):
    """The listed card has no exception, so it must not appear."""
    assert LISTED_CARD not in names(get(f"/inventory?{EXCEPTION_SEARCH}"))


# ---------------------------------------------------------------------
# the links that page and sort must carry it forward
# ---------------------------------------------------------------------

def test_a_sort_link_from_an_exception_search_keeps_every_status(db):
    html = get(f"/inventory?{EXCEPTION_SEARCH}")
    link = next(
        m.group(1) for m in re.finditer(r'href="(/inventory\?sort=[^"]+)"', html)
        if "exception_status" in m.group(1)
    ).replace("&amp;", "&")
    followed = get(link)
    assert names(followed) == {REMOVED_CARD, UNSELLABLE_CARD}
    assert ticked(followed) == set()


def test_the_bulk_action_back_link_keeps_every_status(db):
    html = get(f"/inventory?{EXCEPTION_SEARCH}")
    back = re.search(
        r'name="back_link" value="(/inventory\?[^"]+)"', html,
    ).group(1).replace("&amp;", "&")
    assert "exception_status=exception_unresolved" in back
    followed = get(back)
    assert names(followed) == {REMOVED_CARD, UNSELLABLE_CARD}
    assert ticked(followed) == set()


# ---------------------------------------------------------------------
# what must NOT change
# ---------------------------------------------------------------------

def test_bare_inventory_still_shows_listed_only(db):
    html = get("/inventory")
    assert names(html) == {LISTED_CARD}
    assert ticked(html) == {"listed"}


def test_an_exception_search_that_names_a_status_still_filters(db):
    html = get(f"/inventory?{EXCEPTION_SEARCH}&status=removed")
    assert names(html) == {REMOVED_CARD}
    assert ticked(html) == {"removed"}


def test_an_exception_search_that_names_two_statuses_ors_them(db):
    html = get(f"/inventory?{EXCEPTION_SEARCH}&status=removed&status=sold")
    assert names(html) == {REMOVED_CARD}
    assert ticked(html) == {"sold", "removed"}


def test_a_rejected_exception_value_leaves_the_listed_default_alone(db):
    """`exception_status=not-a-state` is dropped by the route, so there is
    no exception filter and nothing to suppress the default."""
    html = get("/inventory?exception_status=not-a-state")
    assert names(html) == {LISTED_CARD}
    assert ticked(html) == {"listed"}


# ---------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------

def test_suppresses_status_default_reads_the_exception_filter():
    assert svc.suppresses_status_default(exception_status="exception_unresolved")
    assert not svc.suppresses_status_default(exception_status="")
    assert not svc.suppresses_status_default(exception_status=None)
    assert not svc.suppresses_status_default(exception_status="   ")


def test_suppresses_status_default_ignores_a_filter_it_does_not_know():
    """A caller passing one more keyword must not be able to break the
    page, in either direction."""
    assert not svc.suppresses_status_default(set_code="MSH")
    assert svc.suppresses_status_default(
        exception_status="exception_unresolved", set_code="MSH",
    )
    assert not svc.suppresses_status_default()


def test_suppress_default_gives_no_filter_but_never_beats_a_choice():
    assert svc.parse_statuses([], submitted=False, suppress_default=True) == ()
    assert svc.parse_statuses([], submitted=True, suppress_default=True) == ()
    assert svc.parse_statuses(
        ["removed"], submitted=False, suppress_default=True,
    ) == ("removed",)
    assert svc.parse_statuses([], submitted=False) == svc.DEFAULT_STATUSES
