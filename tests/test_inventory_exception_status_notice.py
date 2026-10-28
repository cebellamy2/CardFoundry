"""The notice that says an exception search is narrowed to a status, and
the one-click way out of it (operator decision, 2026-10-28).

★ THE TRAP THIS CLOSES. Listed is pre-ticked on a bare /inventory. An
operator who picks an Exception state in the same form and submits sends
`status=listed` with it -- an explicit choice, which still narrows on
purpose so `?exception_status=...&status=removed` keeps working. But an
exception search narrowed to Listed has historically returned nothing,
and nothing looks exactly like "there are no exceptions" rather than
"your status filter is wrong". The notice names what is being shown and
links to the same search with the status cleared.

Fixtures go through the real mark_fulfillment_exception, so the cards
reach the statuses production puts them in.
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
    engine = create_engine(f"sqlite:///{tmp_path / 'exception-notice.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    with Session(engine) as session:
        _, missing_item, missing_card, missing_allocation = seed(session)
        _, mismatch_item, mismatch_card, mismatch_allocation = seed(session)
        # Both sides of the name: marking an exception crosschecks the
        # order item's identity against the physical card's.
        missing_card.name = missing_item.name = REMOVED_CARD
        mismatch_card.name = mismatch_item.name = UNSELLABLE_CARD
        session.flush()
        mark_fulfillment_exception(session, missing_allocation.id, "missing", "Not found")
        mark_fulfillment_exception(
            session, mismatch_allocation.id, "inventory_mismatch", "Wrong printing",
        )
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
    return {n for n in ALL_NAMES if f'aria-label="Select {n}"' in html}


def ticked(html):
    row = html[html.index('class="filter-checkbox-row"'):]
    row = row[:row.index("</div>")]
    return {m.group(1) for m in re.finditer(r'value="([a-z_]+)"[^>]*checked', row)}


def notice(html):
    """The notice text, or "" when it is not on the page. Scoped to the
    .notice block so an unrelated sentence elsewhere cannot pass for it."""
    marker = '<div class="notice">'
    if marker not in html:
        return ""
    block = html[html.index(marker) + len(marker):]
    return block[:block.index("</div>")]


def every_status_href(html):
    link = re.search(r'<a href="([^"]+)">Show every status</a>', notice(html))
    assert link, "no Show every status link in the notice"
    return link.group(1).replace("&amp;", "&")


EXCEPTION = "exception_status=exception_unresolved"


# ---------------------------------------------------------------------
# (a) the form as the operator submits it
# ---------------------------------------------------------------------

def test_the_form_submission_shows_the_notice_and_the_link_widens_it(db):
    """Listed pre-ticked + an exception state picked, which is exactly
    what the form sends on the operator's first hop."""
    html = get(f"/inventory?{EXCEPTION}&status=listed&{svc.SUBMITTED_MARKER}=1")
    assert names(html) == set(), "the trap: a narrowed search looks empty"
    text = notice(html)
    assert "Only showing Listed cards with this exception state." in text
    assert "usually Removed or Unavailable" in text

    widened = get(every_status_href(html))
    assert names(widened) == {REMOVED_CARD, UNSELLABLE_CARD}
    assert ticked(widened) == set()
    assert notice(widened) == "", "nothing is narrowed any more"


def test_the_link_carries_every_other_filter_and_the_sort(db):
    html = get(
        f"/inventory?{EXCEPTION}&status=listed&batch=LISTED-1"
        "&sort=current_price&direction=desc"
    )
    link = every_status_href(html)
    assert "exception_status=exception_unresolved" in link
    assert "batch=LISTED-1" in link
    assert "sort=current_price" in link
    assert "direction=desc" in link
    assert svc.SUBMITTED_MARKER in link
    assert "status=" not in link.replace("exception_status=", "").replace(
        svc.SUBMITTED_MARKER, "")
    assert "page=" not in link, "a bigger result set starts at page 1"


# ---------------------------------------------------------------------
# (b) a deliberate narrowing still narrows, and is still named
# ---------------------------------------------------------------------

def test_a_removed_only_exception_search_is_narrowed_and_says_so(db):
    html = get(f"/inventory?{EXCEPTION}&status=removed")
    assert names(html) == {REMOVED_CARD}, "an explicit status still wins"
    assert "Only showing Removed cards" in notice(html)
    assert names(get(every_status_href(html))) == {REMOVED_CARD, UNSELLABLE_CARD}


def test_two_narrowed_statuses_are_both_named(db):
    html = get(f"/inventory?{EXCEPTION}&status=removed&status=sold")
    assert "Only showing Sold and Removed cards" in notice(html)


# ---------------------------------------------------------------------
# (c) and (d) one filter alone is not narrowing anything
# ---------------------------------------------------------------------

def test_an_exception_search_with_no_status_has_no_notice(db):
    html = get(f"/inventory?{EXCEPTION}")
    assert notice(html) == ""
    assert names(html) == {REMOVED_CARD, UNSELLABLE_CARD}


def test_a_status_search_with_no_exception_filter_has_no_notice(db):
    assert notice(get("/inventory?status=removed")) == ""


def test_a_bare_inventory_has_no_notice_and_still_defaults_to_listed(db):
    html = get("/inventory")
    assert notice(html) == ""
    assert ticked(html) == {"listed"}
    assert names(html) == {LISTED_CARD}


def test_a_rejected_exception_value_shows_no_notice(db):
    """The route drops it, so there is no exception filter to narrow."""
    assert notice(get("/inventory?exception_status=not-a-state&status=removed")) == ""


def test_no_javascript_was_added_for_the_notice(db):
    html = get(f"/inventory?{EXCEPTION}&status=listed")
    assert "<script" not in notice(html)


# ---------------------------------------------------------------------
# the rule and the wording helper
# ---------------------------------------------------------------------

def test_status_filter_is_narrowing_needs_both_halves():
    assert svc.status_filter_is_narrowing(
        ("listed",), exception_status="exception_unresolved")
    assert not svc.status_filter_is_narrowing(
        (), exception_status="exception_unresolved")
    assert not svc.status_filter_is_narrowing(("listed",), exception_status="")
    assert not svc.status_filter_is_narrowing((), exception_status="")


def test_status_labels_uses_the_pages_own_words_in_display_order():
    assert svc.status_labels(("removed",)) == "Removed"
    assert svc.status_labels(("not_listed", "listed")) == "Listed and Not Listed"
    assert svc.status_labels(("removed", "sold", "reserved")) == (
        "Reserved, Sold and Removed")
    assert svc.status_labels(("unsellable",)) == "Unavailable"
    assert svc.status_labels(()) == ""


def test_status_labels_keeps_a_value_it_cannot_name():
    """Better an ugly sentence than one that silently omits a status it
    is supposed to be explaining."""
    assert svc.status_labels(("removed", "mystery")) == "Removed and mystery"
